"""The public DID documents under `/ids/{slug}`: found only once signed and
published, never the root's (the domain's own DID, under `/.well-known/`)."""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import webvh
from registry_api.models import Identity
from registry_api.root import create_root
from tests.conftest import Dns, Outbox
from tests.fake_wallet import FakeWallet
from tests.mediators import verified_domain
from tests.signing import link_wallet, publish, ready, sign
from tests.test_directory import _sign_in


async def _missing(client: AsyncClient, slug: str) -> None:
    for name in ("did.json", "did.jsonl"):
        response = await client.get(f"/ids/{slug}/{name}")
        assert response.status_code == 404 and response.json()["detail"] == "not_found", name


async def test_an_identity_is_public_once_signed_and_published(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}"
    door = (await client.post(f"{base}/verifiers", json={"name": "Door"}, headers=headers)).json()
    slug = (await db.get_one(Identity, uuid.UUID(door["identity"]["id"]))).slug
    # Pending: nothing signed yet.
    await _missing(client, slug)
    wallet = await ready(client, headers, tenant)
    await sign(client, headers, tenant, door["identity"]["id"], wallet)
    # Signed, but a draft's DID does not resolve.
    await _missing(client, slug)
    await publish(client, headers, tenant, "verifiers", door["id"])

    log = await client.get(f"/ids/{slug}/did.jsonl")
    assert log.status_code == 200 and log.headers["content-type"].startswith("text/jsonl")
    entries = [json.loads(line) for line in log.text.splitlines()]
    document = await client.get(f"/ids/{slug}/did.json")
    assert document.headers["content-type"] == "application/did+json"
    # The document is the log's last state, under its did:web name.
    assert document.json() == webvh.as_did_web(entries[-1]["state"], webvh.scid_of(entries))
    assert document.json()["alsoKnownAs"] == [entries[-1]["state"]["id"]]

    # Taken back, it is gone again.
    await client.post(f"{base}/verifiers/{door['id']}/unpublish", headers=headers)
    await _missing(client, slug)
    await _missing(client, "idn_nothere")


@pytest.mark.no_anchor
async def test_the_roots_identity_is_not_under_ids(
    client: AsyncClient, outbox: Outbox, db: AsyncSession, dns: Dns
) -> None:
    root = await create_root(db, "Almena", "root@almena.id")
    headers, _ = await _sign_in(client, outbox, "root@almena.id")
    wallet = FakeWallet()
    await link_wallet(client, headers, wallet)
    await verified_domain(client, headers, str(root.id), dns)
    assert root.identity_id is not None
    await sign(client, headers, str(root.id), str(root.identity_id), wallet)
    # Signed, it is the domain's own DID: at /.well-known/, never at /ids/.
    assert (await client.get("/.well-known/did.json")).status_code == 200
    await _missing(client, (await db.get_one(Identity, root.identity_id)).slug)
