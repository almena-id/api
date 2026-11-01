"""The wallet's side of an application, where the whole flow
(`test_applications`) does not go: requests unknown, expired or answered,
answers too large or at once, files too large, and the catalog's poll."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from registry_api import answers, bodies
from registry_api.api.routes import application_wallet, applications
from registry_api.models import Application
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.test_applications import _ask, _offer


async def _started(client: AsyncClient, outbox: Outbox) -> tuple[str, dict[str, str]]:
    _, club, _, _ = await _offer(client, outbox)
    items = (await client.get("/api/v1/catalog/issuers")).json()["items"]
    slug = next(item["slug"] for item in items if item["did"] == club.did)
    started = (
        await client.post("/api/v1/applications", json={"issuer": slug, "type": "membership"})
    ).json()
    return f"/api/v1/applications/{started['id']}", {"X-Application-Secret": started["secret"]}


async def _paired(
    client: AsyncClient, outbox: Outbox
) -> tuple[str, dict[str, str], FakeWallet, dict[str, Any]]:
    base, secret = await _started(client, outbox)
    holder = FakeWallet()
    pairing = await _ask(client, base, secret, "pair")
    answered = await client.post(
        urlparse(pairing["response_uri"]).path, data={"id_token": holder.id_token(pairing)}
    )
    assert answered.status_code == 204, answered.text
    return base, secret, holder, pairing


async def test_the_catalog_polls_and_the_wallet_reads_once(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    base, secret = await _started(client, outbox)
    unknown = await client.get(f"/api/v1/applications/{uuid.uuid4()}/request")
    assert unknown.status_code == 404 and unknown.json()["detail"] == "request_not_found"
    # Nothing asked yet.
    none = await client.get(f"{base}/request")
    assert none.status_code == 404 and none.json()["detail"] == "request_not_found"
    assert (await client.get(f"{base}/wallet", headers=secret)).json()["purpose"] is None

    pairing = await _ask(client, base, secret, "pair")
    state = (await client.get(f"{base}/wallet", headers=secret)).json()
    assert state["purpose"] == "pair" and state["live"] is True and state["deep_link"]
    assert state["answered"] is False
    path = urlparse(pairing["response_uri"]).path
    await client.post(path, data={"id_token": FakeWallet().id_token(pairing)})
    state = (await client.get(f"{base}/wallet", headers=secret)).json()
    assert state["answered"] is True and state["deep_link"] is None
    read = await client.get(path.removesuffix("/response"))
    assert read.status_code == 409 and read.json()["detail"] == "already_answered"
    # The poll is the secret holder's alone.
    wrong = await client.get(f"{base}/wallet", headers={"X-Application-Secret": "nope"})
    assert wrong.status_code == 404

    present = await _ask(client, base, secret, "present")
    item = await db.get_one(Application, uuid.UUID(base.rsplit("/", 1)[1]))
    item.wallet_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    path = urlparse(present["response_uri"]).path
    expired = await client.get(path.removesuffix("/response"))
    assert expired.status_code == 410 and expired.json()["detail"] == "request_expired"
    late = await client.post(path, json={"vp_token": {}})
    assert late.status_code == 410 and late.json()["detail"] == "request_expired"
    assert (await client.get(f"{base}/wallet", headers=secret)).json()["live"] is False


async def test_an_answer_too_large_is_refused_and_the_request_left_open(
    client: AsyncClient, outbox: Outbox
) -> None:
    base, secret = await _started(client, outbox)
    pairing = await _ask(client, base, secret, "pair")
    path = urlparse(pairing["response_uri"]).path
    huge = {"id_token": "x" * (bodies.PRESENTATION_MOST + 1)}
    refused = await client.post(path, data=huge)
    assert refused.status_code == 413 and refused.json()["detail"] == "body_too_large"
    assert refused.json()["max_bytes"] == bodies.PRESENTATION_MOST
    paired = await client.post(path, data={"id_token": FakeWallet().id_token(pairing)})
    assert paired.status_code == 204, paired.text


async def test_of_two_answers_at_once_only_one_is_taken(
    client: AsyncClient, outbox: Outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second read the request before the first was kept: the update
    that takes it finds it answered, and the first wallet stays paired."""
    base, secret = await _started(client, outbox)
    pairing = await _ask(client, base, secret, "pair")
    path = urlparse(pairing["response_uri"]).path
    first = FakeWallet()
    assert (await client.post(path, data={"id_token": first.id_token(pairing)})).status_code == 204

    async def stale(db: AsyncSession, application_id: uuid.UUID) -> Application:
        item = await db.get_one(Application, application_id)
        # As read before the first answer was committed.
        set_committed_value(item, "wallet_answered", False)
        return item

    monkeypatch.setattr(application_wallet, "_asked", stale)
    raced = await client.post(path, data={"id_token": FakeWallet().id_token(pairing)})
    assert raced.status_code == 409 and raced.json()["detail"] == "already_answered"
    monkeypatch.undo()
    assert (await client.get(base, headers=secret)).json()["holder_did"] == first.did


async def test_a_file_too_large_says_how_large_it_may_be(
    client: AsyncClient, outbox: Outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, secret, _, _ = await _paired(client, outbox)
    # A smaller limit, so the test sends little.
    monkeypatch.setattr(applications, "MAX_FILE", 16)
    refused = await client.post(
        f"{base}/files",
        data={"key": "id_scan"},
        files={"file": ("id.pdf", b"%PDF-1.7 " + b"x" * 16, "application/pdf")},
        headers=secret,
    )
    assert refused.status_code == 413
    assert refused.json() == {"detail": "file_too_large", "max_bytes": 16}
    monkeypatch.undo()
    # The real limit, as the offer says it.
    view = (await client.get(base, headers=secret)).json()
    scan = next(f for f in view["offer"]["form"]["fields"] if f["key"] == "id_scan")
    assert scan["max_bytes"] == answers.MAX_FILE
