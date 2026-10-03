import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from httpx import AsyncClient

from registry_api import didcomm
from registry_api.main import app
from registry_api.messaging_keys import multikey
from registry_api.wallet import b58encode
from tests.conftest import Outbox
from tests.test_issuance import (
    CLAIMS,
    _accepted,
    answer_jws,
    read_sign_request,
    sign_status_list,
)

MEDIATOR = "did:web:mediator.example.org"


def _raw_x(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes_raw()


@dataclass
class Holder:
    """A wallet's messaging side: a did:peer:2 behind the test mediator."""

    key: X25519PrivateKey = field(default_factory=X25519PrivateKey.generate)
    signing: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)

    @property
    def did(self) -> str:
        signing = self.signing.public_key().public_bytes_raw()
        service = {"t": "dm", "s": {"uri": MEDIATOR, "a": ["didcomm/v2"]}}
        return (
            "did:peer:2"
            f".Vz{b58encode(b'\\xed\\x01' + signing)}"
            f".E{multikey(_raw_x(self.key))}"
            f".S{didcomm.b64(json.dumps(service).encode())}"
        )


@dataclass
class Mediator:
    """A mediator that keeps what it is handed."""

    key: X25519PrivateKey = field(default_factory=X25519PrivateKey.generate)
    received: list[tuple[str, str]] = field(default_factory=list)
    status: int = 202

    def document(self) -> dict[str, Any]:
        return {
            "id": MEDIATOR,
            "verificationMethod": [
                {
                    "id": f"{MEDIATOR}#key-x25519",
                    "type": "JsonWebKey2020",
                    "controller": MEDIATOR,
                    "publicKeyJwk": {
                        "kty": "OKP",
                        "crv": "X25519",
                        "x": didcomm.b64(_raw_x(self.key)),
                    },
                }
            ],
            "keyAgreement": [f"{MEDIATOR}#key-x25519"],
            "service": [
                {
                    "id": f"{MEDIATOR}#didcomm",
                    "type": "DIDCommMessaging",
                    "serviceEndpoint": [
                        {"uri": "https://mediator.example.org/didcomm", "accept": ["didcomm/v2"]},
                        {"uri": "wss://mediator.example.org/ws", "accept": ["didcomm/v2"]},
                    ],
                }
            ],
        }

    def courier(self) -> didcomm.Courier:
        async def fetch(url: str) -> dict[str, Any]:
            assert url == "https://mediator.example.org/.well-known/did.json"
            return self.document()

        async def post(url: str, envelope: str) -> int:
            self.received.append((url, envelope))
            return self.status

        return didcomm.Courier(fetch=fetch, post=post)

    def opened(
        self, holder: Holder, did: str, sender: bytes
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The last envelope: its forward, and the message inside for `holder`."""
        _, envelope = self.received[-1]
        forward = json.loads(
            didcomm.decrypt(json.loads(envelope), f"{MEDIATOR}#key-x25519", self.key)
        )
        inner = forward["attachments"][0]["data"]["json"]
        plain = json.loads(didcomm.decrypt(inner, f"{did}#key-2", holder.key, sender))
        return forward, plain


@pytest.fixture
def mediator() -> Mediator:
    found = Mediator()
    app.dependency_overrides[didcomm.get_courier] = found.courier
    return found


def test_authcrypt_and_anoncrypt_round_trip() -> None:
    alice, bob, eve = (X25519PrivateKey.generate() for _ in range(3))
    sealed = didcomm.encrypt(b"hi", [("bob#1", _raw_x(bob))], ("alice#1", alice))
    header = json.loads(didcomm.unb64(sealed["protected"]))
    assert header["alg"] == "ECDH-1PU+A256KW" and header["enc"] == "A256CBC-HS512"
    assert header["skid"] == "alice#1" and didcomm.unb64(header["apu"]) == b"alice#1"
    assert didcomm.decrypt(sealed, "bob#1", bob, _raw_x(alice)) == b"hi"
    # Bound to its sender: another's key does not open it.
    with pytest.raises(Exception):  # noqa: B017
        didcomm.decrypt(sealed, "bob#1", bob, _raw_x(eve))
    anon = didcomm.encrypt(b"hi", [("bob#1", _raw_x(bob)), ("eve#1", _raw_x(eve))])
    assert didcomm.decrypt(anon, "eve#1", eve) == b"hi"
    # The recipients are bound too (`apv`).
    anon["recipients"].pop()
    with pytest.raises(didcomm.DidCommError):
        didcomm.decrypt(anon, "bob#1", bob)


def test_a_holders_did_peer_says_where_it_receives() -> None:
    holder = Holder()
    found = didcomm.peer(holder.did)
    assert found.kid == f"{holder.did}#key-2" and found.key == _raw_x(holder.key)
    assert found.service == MEDIATOR
    for bad in (
        "did:key:z6Mk",
        "did:peer:2.Ez6LSbad",
        "did:peer:2.Vz6Mk",
        "did:peer:2." + "x" * 3000,
    ):
        with pytest.raises(didcomm.DidCommError):
            didcomm.peer(bad)


def test_where_a_did_web_document_is() -> None:
    assert didcomm.did_web_url(MEDIATOR) == "https://mediator.example.org/.well-known/did.json"
    assert (
        didcomm.did_web_url("did:web:localhost%3A8080")
        == "http://localhost:8080/.well-known/did.json"
    )
    assert didcomm.did_web_url("did:web:almena.id:ids:abc") == "https://almena.id/ids/abc/did.json"


async def _issuer_key(client: AsyncClient, did: str) -> bytes:
    """The issuer's X25519 key, as its published did:web document lists it."""
    slug = did.rsplit(":", 1)[1]
    document = (await client.get(f"/ids/{slug}/did.json")).json()
    kid = document["keyAgreement"][0]
    return didcomm.x25519_of(kid.split("#")[1])


async def test_the_holder_hears_of_the_decision_and_of_the_issuance(
    client: AsyncClient, outbox: Outbox, mediator: Mediator
) -> None:
    holder = Holder()
    did = holder.did
    club, headers, tenant, application_id, _, wallet = await _accepted(client, outbox, didcomm=did)
    # Accepted: the club's notice reached the holder's mediator.
    url, _ = mediator.received[-1]
    assert url == "https://mediator.example.org/didcomm"
    sender = await _issuer_key(client, club.did)
    forward, notice = mediator.opened(holder, did, sender)
    assert forward["type"] == didcomm.FORWARD and forward["body"] == {"next": did}
    assert forward["to"] == [MEDIATOR]
    web = "did:web:" + club.did.split(":", 3)[3]
    assert notice["type"] == "https://almena.id/protocols/application/1.0/status"
    assert notice["from"] == web and notice["to"] == [did]
    assert notice["thid"] == application_id
    assert notice["body"] == {
        "application": application_id,
        "status": "accepted",
        "credential_type": "membership",
        "credential_name": {"en": "Membership", "es": "Afiliación"},
        "issuer": "Club",
    }

    # Issued: the notice says where the wallet collects it, and it can.
    await sign_status_list(client, headers, tenant, club.wallet)
    issuing = f"/api/v1/tenants/{tenant}/applications/{application_id}/issuance"
    await client.put(issuing, json={"claims": CLAIMS, "valid_until": "2030-12-31"}, headers=headers)
    asked = await client.post(f"{issuing}/sign", json={"locale": "en"}, headers=headers)
    answered = await answer_jws(client, await read_sign_request(client, asked.json()), club.wallet)
    assert answered.status_code == 204, answered.text
    _, issued = mediator.opened(holder, did, sender)
    assert issued["body"]["status"] == "issued"
    collect = urlparse(issued["body"]["collect"]).path
    assert collect == f"/api/v1/applications/{application_id}/collect"

    def proof(signer: Any, nonce: str) -> str:
        now = int(time.time())
        audience = issued["body"]["collect"]
        claims = {"iss": signer.did, "sub": signer.did, "aud": audience}
        return jwt.encode(
            {**claims, "nonce": nonce, "iat": now, "exp": now + 120}, signer.key, algorithm="EdDSA"
        )

    stranger = type(wallet)()
    refused = await client.post(collect, json={"id_token": proof(stranger, application_id)})
    assert refused.status_code == 404
    wrong = await client.post(collect, json={"id_token": proof(wallet, "other")})
    assert wrong.status_code == 400 and wrong.json()["detail"] == "invalid_token"
    asked = await client.post(collect, json={"id_token": proof(wallet, application_id)})
    assert asked.status_code == 200, asked.text
    request_uri = asked.json()["request_uri"]
    assert unquote(parse_qs(urlparse(asked.json()["deep_link"]).query)["request_uri"][0]) == (
        request_uri
    )
    receive = (await client.get(urlparse(request_uri).path)).json()
    assert receive["purpose"] == "receive"
    taken = await client.post(
        urlparse(receive["response_uri"]).path, data={"id_token": wallet.id_token(receive)}
    )
    assert taken.status_code == 200, taken.text
    again = await client.post(collect, json={"id_token": proof(wallet, application_id)})
    assert again.status_code == 409 and again.json()["detail"] == "already_received"


async def test_a_notice_is_never_a_condition(
    client: AsyncClient, outbox: Outbox, mediator: Mediator
) -> None:
    # The mediator refuses it: the decision stands all the same.
    mediator.status = 507
    holder = Holder()
    _, headers, tenant, application_id, _, _ = await _accepted(client, outbox, didcomm=holder.did)
    assert len(mediator.received) == 1
    inbox = f"/api/v1/tenants/{tenant}/applications/{application_id}"
    assert (await client.get(inbox, headers=headers)).json()["status"] == "accepted"


async def test_a_wallet_that_names_no_did_hears_nothing(
    client: AsyncClient, outbox: Outbox, mediator: Mediator
) -> None:
    await _accepted(client, outbox)
    assert mediator.received == []


async def test_what_a_wallet_names_must_be_a_did_peer(
    client: AsyncClient, outbox: Outbox, mediator: Mediator
) -> None:
    with pytest.raises(AssertionError, match="invalid_didcomm"):
        await _accepted(
            client, outbox, didcomm="did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
        )
