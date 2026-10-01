import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import jwt
from httpx import AsyncClient

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
    client: AsyncClient, outbox: Outbox
) -> tuple[Issuer, dict[str, str], str, str, dict[str, str], FakeWallet]:
    """An application, sent and accepted: the club, its member's headers and
    tenant, the application's id, its secret and the holder's wallet."""
    mailer, club, headers, tenant = await _offer(client, outbox)
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
    await client.post(
        urlparse(pairing["response_uri"]).path, data={"id_token": holder.id_token(pairing)}
    )
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

    # And it holds: presented, with the holder's key, it is verified.
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
