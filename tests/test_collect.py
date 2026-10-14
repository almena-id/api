"""The holder's wallet naming where it receives (`didcomm`, at pairing) and
collecting an issued credential (`POST /applications/{id}/collect`)."""

import base64
import json
import uuid
from urllib.parse import urlparse

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.models import Application
from registry_api.wallet import b58encode
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.test_applications import _ask, _offer, _request
from tests.test_issuance import CLAIMS, _accepted, answer_jws, read_sign_request, sign_status_list

COLLECT = "https://almena.id/api/v1/applications/{}/collect"


def _peer() -> str:
    """A did:peer:2 as a wallet names it: an X25519 key and its mediator."""
    key = "z" + b58encode(b"\xec\x01" + bytes(32))
    service = {"t": "dm", "s": {"uri": "did:web:mediator.example.org", "a": ["didcomm/v2"]}}
    encoded = base64.urlsafe_b64encode(json.dumps(service).encode()).rstrip(b"=").decode()
    return f"did:peer:2.E{key}.S{encoded}"


async def test_pairing_keeps_the_did_peer_the_wallet_names(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    peer = _peer()
    *_, application_id, _secret, _holder = await _accepted(client, outbox, didcomm=peer)
    item = await db.get(Application, uuid.UUID(application_id))
    assert item is not None and item.holder_messaging_did == peer


async def test_pairing_refuses_what_is_not_a_did_peer(client: AsyncClient, outbox: Outbox) -> None:
    _mailer, club, _headers, _tenant = await _offer(client, outbox)
    slug = next(
        item["slug"]
        for item in (await client.get("/api/v1/catalog/issuers")).json()["items"]
        if item["did"] == club.did
    )
    for didcomm in ("did:key:z6Mkhello", "did:peer:2.Vz6Mk", 42):
        started = (
            await client.post("/api/v1/applications", json={"issuer": slug, "type": "membership"})
        ).json()
        base = f"/api/v1/applications/{started['id']}"
        pairing = await _ask(client, base, {"X-Application-Secret": started["secret"]}, "pair")
        paired = await client.post(
            urlparse(pairing["response_uri"]).path,
            data={"id_token": FakeWallet().id_token(pairing, didcomm=didcomm)},
        )
        assert paired.status_code == 400 and paired.json()["detail"] == "invalid_didcomm"


async def test_the_holder_collects_its_issued_credential(
    client: AsyncClient, outbox: Outbox
) -> None:
    club, headers, tenant, application_id, _secret, holder = await _accepted(client, outbox)
    url = f"/api/v1/applications/{application_id}/collect"
    asked = {"client_id": COLLECT.format(application_id), "nonce": application_id}

    # Not before it is issued.
    early = await client.post(url, json={"id_token": holder.id_token(asked)})
    assert early.status_code == 409 and early.json()["detail"] == "not_issued"

    await sign_status_list(client, headers, tenant, club.wallet)
    issuing = f"/api/v1/tenants/{tenant}/applications/{application_id}/issuance"
    await client.put(issuing, json={"claims": CLAIMS, "valid_until": "2030-12-31"}, headers=headers)
    sign = await client.post(f"{issuing}/sign", json={"locale": "en"}, headers=headers)
    await answer_jws(client, await read_sign_request(client, sign.json()), club.wallet)

    # Only the holder, signing for this very endpoint.
    stranger = await client.post(url, json={"id_token": FakeWallet().id_token(asked)})
    assert stranger.status_code == 404
    elsewhere = {**asked, "client_id": "https://api.almena.id/elsewhere"}
    wrong = await client.post(url, json={"id_token": holder.id_token(elsewhere)})
    assert wrong.status_code == 400

    collected = await client.post(url, json={"id_token": holder.id_token(asked)})
    assert collected.status_code == 200, collected.text
    receive = await _request(client, collected.json()["deep_link"])
    assert receive["purpose"] == "receive"
    taken = await client.post(
        urlparse(receive["response_uri"]).path, data={"id_token": holder.id_token(receive)}
    )
    assert taken.status_code == 200, taken.text
    again = await client.post(url, json={"id_token": holder.id_token(asked)})
    assert again.status_code == 409 and again.json()["detail"] == "already_received"


async def test_collecting_an_unknown_application(client: AsyncClient) -> None:
    unknown = str(uuid.uuid4())
    asked = {"client_id": COLLECT.format(unknown), "nonce": unknown}
    response = await client.post(
        f"/api/v1/applications/{unknown}/collect",
        json={"id_token": FakeWallet().id_token(asked)},
    )
    assert response.status_code == 404 and response.json()["detail"] == "application_not_found"
