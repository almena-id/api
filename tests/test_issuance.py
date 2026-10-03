import base64
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import jwt
from httpx import AsyncClient

from registry_api.main import app
from registry_api.presentations import get_status_fetch
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.test_applications import _ask, _offer, _sign_submission
from tests.test_presentations import Issuer, sd_jwt

ANSWERS = {
    "given_name": "Lucía",
    "birthdate": "1990-03-14",
    "address": {
        "street_address": "Calle Mayor 3",
        "locality": "Sevilla",
        "postal_code": "41001",
        "country": "ES",
    },
}


async def _accepted(
    client: AsyncClient,
    outbox: Outbox,
    offered: Callable[[dict[str, str], str], Awaitable[Any]] | None = None,
    **pairing_claims: Any,
) -> tuple[Issuer, dict[str, str], str, str, dict[str, str], FakeWallet]:
    """An application, sent and accepted: the club, its member's headers and
    tenant, the application's id, its secret and the holder's wallet (which
    pairs saying `pairing_claims` too). `offered` is called with the club's
    headers and tenant once it offers, before anybody applies."""
    mailer, club, headers, tenant = await _offer(client, outbox)
    if offered is not None:
        await offered(headers, tenant)
    slug = next(
        item["slug"]
        for item in (await client.get("/api/v1/catalog/issuers")).json()["items"]
        if item["did"] == club.did
    )
    started = (
        await client.post("/api/v1/applications", json={"issuer": slug, "type": "membership"})
    ).json()
    base = f"/api/v1/applications/{started['id']}"
    secret = {"X-Application-Secret": started["secret"]}
    holder = FakeWallet()
    pairing = await _ask(client, base, secret, "pair")
    paired = await client.post(
        urlparse(pairing["response_uri"]).path,
        data={"id_token": holder.id_token(pairing, **pairing_claims)},
    )
    assert paired.status_code == 204, paired.text
    asked = await _ask(client, base, secret, "present")
    email = sd_jwt(
        mailer,
        FakeWallet(),
        {"email": "lucia@example.org"},
        vct="https://almena.id/credentials/verified_email/v1",
        nonce=asked["nonce"],
        aud=asked["client_id"],
    )
    await client.post(
        urlparse(asked["response_uri"]).path,
        json={"vp_token": {"verified_email_sd_jwt": [email]}},
    )
    await client.post(
        f"{base}/files",
        data={"key": "id_scan"},
        files={"file": ("id.pdf", b"%PDF-1.7", "application/pdf")},
        headers=secret,
    )
    saved = await client.put(f"{base}/answers", json={"answers": ANSWERS}, headers=secret)
    assert saved.status_code == 200, saved.text
    submit = await _ask(client, base, secret, "submit")
    await client.post(
        urlparse(submit["response_uri"]).path,
        json={"signature": _sign_submission(holder, submit)},
    )
    inbox = f"/api/v1/tenants/{tenant}/applications/{started['id']}"
    decided = await client.post(f"{inbox}/decision", json={"decision": "accepted"}, headers=headers)
    assert decided.json()["status"] == "accepted"
    return club, headers, tenant, started["id"], secret, holder


def _sign_credential(signer: FakeWallet, document: dict[str, Any]) -> str:
    return jwt.encode(
        document["payload"], signer.key, algorithm="EdDSA", headers=document["header"]
    )


async def read_sign_request(client: AsyncClient, asked: dict[str, Any]) -> dict[str, Any]:
    """The wallet request a portal was handed, as the wallet reads it."""
    uri = unquote(parse_qs(urlparse(asked["deep_link"]).query)["request_uri"][0])
    request: dict[str, Any] = (await client.get(urlparse(uri).path)).json()
    return request


async def answer_jws(client: AsyncClient, request: dict[str, Any], signer: FakeWallet) -> Any:
    """The signer's wallet signs what the request carries, as a JWS."""
    path = urlparse(request["response_uri"]).path
    jws = _sign_credential(signer, request["sign"]["document"])
    return await client.post(path, json={"jws": jws})


async def issuer_id_of(client: AsyncClient, headers: dict[str, str], tenant: str) -> str:
    issuers = (await client.get(f"/api/v1/tenants/{tenant}/issuers", headers=headers)).json()
    return str(issuers["items"][0]["id"])


async def sign_status_list(
    client: AsyncClient, headers: dict[str, str], tenant: str, signer: FakeWallet
) -> dict[str, Any]:
    """The issuer's signer signs its current status list; its request."""
    issuer_id = await issuer_id_of(client, headers, tenant)
    asked = await client.post(
        f"/api/v1/tenants/{tenant}/issuers/{issuer_id}/status-lists/sign",
        json={"locale": "en"},
        headers=headers,
    )
    assert asked.status_code == 200, asked.text
    request = await read_sign_request(client, asked.json())
    answered = await answer_jws(client, request, signer)
    assert answered.status_code == 204, answered.text
    return request


CLAIMS = {
    "given_name": "Lucía",
    "family_name": "García",
    "organization_name": "Club",
    "member_number": "0042",
}


async def issued(
    client: AsyncClient, outbox: Outbox
) -> tuple[Issuer, dict[str, str], str, str, dict[str, str], FakeWallet, str]:
    """An application whose credential was issued and received: as `_accepted`,
    and the credential."""
    club, headers, tenant, application_id, secret, holder = await _accepted(client, outbox)
    await sign_status_list(client, headers, tenant, club.wallet)
    issuing = f"/api/v1/tenants/{tenant}/applications/{application_id}/issuance"
    await client.put(issuing, json={"claims": CLAIMS, "valid_until": "2030-12-31"}, headers=headers)
    asked = await client.post(f"{issuing}/sign", json={"locale": "en"}, headers=headers)
    assert asked.status_code == 200, asked.text
    answered = await answer_jws(client, await read_sign_request(client, asked.json()), club.wallet)
    assert answered.status_code == 204, answered.text
    base = f"/api/v1/applications/{application_id}"
    receive = await _ask(client, base, secret, "receive")
    taken = await client.post(
        urlparse(receive["response_uri"]).path, data={"id_token": holder.id_token(receive)}
    )
    assert taken.status_code == 200, taken.text
    credential = taken.json()["credential"]
    return club, headers, tenant, application_id, secret, holder, credential


async def test_the_issuers_signer_issues_and_the_holder_receives_it(
    client: AsyncClient, outbox: Outbox
) -> None:
    club, headers, tenant, application_id, secret, holder = await _accepted(client, outbox)
    issuing = f"/api/v1/tenants/{tenant}/applications/{application_id}/issuance"

    # What the credential would say, proposed from what the holder sent.
    proposal = (await client.get(issuing, headers=headers)).json()
    assert proposal["can_sign"] is True and proposal["holder_did"] == holder.did
    proposed = {c["field"]["id"]: c["value"] for c in proposal["claims"]}
    assert proposed["given_name"] == "Lucía" and proposed["organization_name"] == "Club"
    assert proposed["member_number"] is None

    # The issuer settles the claims; each is checked against its field.
    missing = await client.put(
        issuing,
        json={"claims": {"given_name": "Lucía"}, "valid_until": "2030-12-31"},
        headers=headers,
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["errors"] == {
        "family_name": "required",
        "organization_name": "required",
        "member_number": "required",
    }
    past = await client.put(
        issuing, json={"claims": {}, "valid_until": "2020-01-01"}, headers=headers
    )
    assert past.status_code == 422 and past.json()["detail"] == "valid_until_invalid"
    claims = {
        "given_name": "Lucía",
        "family_name": "García",
        "organization_name": "Club",
        "member_number": "0042",
    }
    saved = await client.put(
        issuing, json={"claims": claims, "valid_until": "2030-12-31"}, headers=headers
    )
    assert saved.status_code == 200, saved.text

    # Not before the issuer's status list is signed: the credential names it.
    assert (await client.get(issuing, headers=headers)).json()["status_list_ready"] is False
    unsigned = await client.post(f"{issuing}/sign", json={"locale": "en"}, headers=headers)
    assert unsigned.status_code == 409 and unsigned.json()["detail"] == "status_list_unsigned"
    listed = await sign_status_list(client, headers, tenant, club.wallet)
    list_uri = listed["sign"]["document"]["payload"]["sub"]
    assert (await client.get(issuing, headers=headers)).json()["status_list_ready"] is True

    # Its signer's wallet signs the SD-JWT VC the registry built.
    asked = await client.post(f"{issuing}/sign", json={"locale": "en"}, headers=headers)
    assert asked.status_code == 200, asked.text
    uri = unquote(parse_qs(urlparse(asked.json()["deep_link"]).query)["request_uri"][0])
    request = (await client.get(urlparse(uri).path)).json()
    sign = request["sign"]
    assert sign["kind"] == "credential" and sign["did"] == club.did
    assert sign["valid_until"] == "2030-12-31T23:59:59Z"
    document = sign["document"]
    assert document["header"]["kid"] == f"{club.did}#{sign['signers'][0]}"
    payload = document["payload"]
    assert payload["vct"] == "https://almena.id/credentials/membership/v1"
    assert payload["cnf"] == {"kid": holder.did} and payload["iss"] == club.did
    assert payload["status"]["status_list"]["uri"] == list_uri
    assert 0 <= payload["status"]["status_list"]["idx"] < 2**17
    # Every claim is a disclosure the payload lists.
    disclosed = {}
    for text in document["disclosures"]:
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).decode()
        assert digest.rstrip("=") in payload["_sd"]
        _, name, value = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        disclosed[name] = value
    assert disclosed == claims

    path = urlparse(request["response_uri"]).path
    forged = await client.post(path, json={"jws": _sign_credential(FakeWallet(), document)})
    assert forged.status_code == 400
    altered = {**document, "payload": {**payload, "vct": "x"}}
    tampered = await client.post(path, json={"jws": _sign_credential(club.wallet, altered)})
    assert tampered.status_code == 400 and tampered.json()["detail"] == "invalid_signature"
    signed = await client.post(path, json={"jws": _sign_credential(club.wallet, document)})
    assert signed.status_code == 204, signed.text
    again = await client.post(f"{issuing}/sign", json={"locale": "en"}, headers=headers)
    assert again.status_code == 409 and again.json()["detail"] == "already_issued"

    # QR 3: the holder's wallet takes it, proving the key it is bound to.
    base = f"/api/v1/applications/{application_id}"
    state = (await client.get(base, headers=secret)).json()
    assert state["status"] == "issued" and state["valid_until"].startswith("2030-12-31")
    receive = await _ask(client, base, secret, "receive")
    assert receive["response_type"] == "credential"
    stranger = FakeWallet()
    refused = await client.post(
        urlparse(receive["response_uri"]).path, data={"id_token": stranger.id_token(receive)}
    )
    assert refused.status_code == 400 and refused.json()["detail"] == "not_the_holder"
    taken = await client.post(
        urlparse(receive["response_uri"]).path, data={"id_token": holder.id_token(receive)}
    )
    assert taken.status_code == 200, taken.text
    delivered = taken.json()
    assert delivered["format"] == "dc+sd-jwt" and delivered["issuer"]["did"] == club.did
    credential = delivered["credential"]
    assert credential.count("~") == len(document["disclosures"]) + 1
    assert (await client.get(base, headers=secret)).json()["delivered_at"]
    # Received once.
    twice = await client.post(f"{base}/wallet", json={"purpose": "receive"}, headers=secret)
    assert twice.status_code == 409 and twice.json()["detail"] == "already_received"

    # And it holds: presented, with the holder's key, it is verified — its
    # status list fetched from the registry.
    async def fetch(url: str) -> str:
        return (await client.get(urlparse(url).path)).text

    app.dependency_overrides[get_status_fetch] = lambda: fetch
    forms = f"/api/v1/tenants/{tenant}/forms"
    form = (
        await client.post(
            forms,
            json={
                "name": {"en": "Members only"},
                "fields": [{"ref": "given_name"}],
                "credentials": [{"type": "membership"}],
            },
            headers=headers,
        )
    ).json()
    presented = credential + jwt.encode(
        {
            "iat": int(time.time()),
            "aud": "https://shop.example",
            "nonce": "n-1",
            "sd_hash": base64.urlsafe_b64encode(hashlib.sha256(credential.encode()).digest())
            .decode()
            .rstrip("="),
        },
        holder.key,
        algorithm="EdDSA",
        headers={"typ": "kb+jwt"},
    )
    verified = (
        await client.post(
            f"{forms}/{form['id']}/verify",
            json={
                "vp_token": {"membership_sd_jwt": [presented]},
                "nonce": "n-1",
                "audience": "https://shop.example",
            },
            headers=headers,
        )
    ).json()
    assert verified["verified"] is True, verified
    assert verified["credentials"][0]["fills"] == {"given_name": "Lucía"}


async def test_only_the_issuers_signer_signs(client: AsyncClient, outbox: Outbox) -> None:
    _, headers, tenant, application_id, _, _ = await _accepted(client, outbox)
    issuing = f"/api/v1/tenants/{tenant}/applications/{application_id}/issuance"
    early = await client.post(f"{issuing}/sign", json={"locale": "en"}, headers=headers)
    assert early.status_code == 409 and early.json()["detail"] == "no_draft"
    await client.put(
        issuing,
        json={
            "claims": {
                "given_name": "A",
                "family_name": "B",
                "organization_name": "C",
                "member_number": "1",
            },
            "valid_until": "2030-01-01",
        },
        headers=headers,
    )
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "admin"},
        headers=headers,
    )
    from tests.test_directory import _sign_in

    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    assert (await client.get(issuing, headers=bob)).json()["can_sign"] is False
    refused = await client.post(f"{issuing}/sign", json={"locale": "en"}, headers=bob)
    assert refused.status_code == 403 and refused.json()["detail"] == "not_the_issuers_signer"
    # Nor settles the draft.
    draft = await client.put(
        issuing,
        json={"claims": {"given_name": "Z"}, "valid_until": "2030-01-01"},
        headers=bob,
    )
    assert draft.status_code == 403 and draft.json()["detail"] == "not_the_issuers_signer"
