import hashlib
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import jwt
from httpx import AsyncClient

from registry_api import webvh
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.test_presentations import Issuer, _issuer, sd_jwt

PORTAL = "https://registry.almena.id"


async def _offer(client: AsyncClient, outbox: Outbox) -> tuple[Issuer, Issuer, dict[str, str], str]:
    """A mail checker granting verified emails, and a club offering memberships
    with a form that asks for one."""
    mailer = await _issuer(client, outbox, "mail@example.org", ["verified_email"])
    club = await _issuer(client, outbox, "club@example.org", ["membership"])
    headers, tenant = await _club_tenant(client, outbox)
    form = (
        await client.post(
            f"/api/v1/tenants/{tenant}/forms",
            json={
                "name": {"en": "Membership"},
                "fields": [
                    {"ref": "given_name"},
                    {"ref": "email"},
                    {"ref": "birthdate", "narrow": {"max_date": "2010-01-01"}},
                    {"ref": "address"},
                    {"ref": "nationalities", "required": False},
                    {"ref": "document_file", "as": "id_scan", "narrow": {"values": ["pdf"]}},
                ],
                "credentials": [{"type": "verified_email"}],
            },
            headers=headers,
        )
    ).json()
    issuers = (await client.get(f"/api/v1/tenants/{tenant}/issuers", headers=headers)).json()
    issuer_id = issuers["items"][0]["id"]
    saved = await client.put(
        f"/api/v1/tenants/{tenant}/issuers/{issuer_id}/credential-types",
        json={"types": ["membership"], "forms": {"membership": form["id"]}},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    return mailer, club, headers, tenant


async def _club_tenant(client: AsyncClient, outbox: Outbox) -> tuple[dict[str, str], str]:
    from tests.test_directory import _sign_in

    return await _sign_in(client, outbox, "club@example.org")


async def _request(client: AsyncClient, deep_link: str) -> dict[str, Any]:
    uri = unquote(parse_qs(urlparse(deep_link).query)["request_uri"][0])
    response = await client.get(urlparse(uri).path)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _ask(
    client: AsyncClient, base: str, secret: dict[str, str], purpose: str
) -> dict[str, Any]:
    response = await client.post(f"{base}/wallet", json={"purpose": purpose}, headers=secret)
    assert response.status_code == 200, response.text
    return await _request(client, response.json()["deep_link"])


def _sign_submission(holder: FakeWallet, request: dict[str, Any], **change: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": holder.did,
        "aud": request["client_id"],
        "nonce": request["nonce"],
        "iat": now,
        "exp": now + 120,
        "application": request["submission"]["content"]["application"],
        "digest": request["submission"]["digest"],
        **change,
    }
    return jwt.encode(claims, holder.key, algorithm="EdDSA")


async def test_a_holder_applies_and_the_issuer_decides(client: AsyncClient, outbox: Outbox) -> None:
    mailer, club, headers, tenant = await _offer(client, outbox)
    slug = (await client.get("/api/v1/catalog/issuers")).json()["items"]
    offered = next(item for item in slug if item["did"] == club.did)
    assert offered["offers"] == ["membership"]
    offer = await client.get(f"/api/v1/catalog/issuers/{offered['slug']}/offers/membership")
    assert offer.status_code == 200
    view = offer.json()
    assert view["issuer"]["did"] == club.did and view["credential_type"]["id"] == "membership"
    assert [f["key"] for f in view["form"]["fields"]][-1] == "id_scan"
    assert view["form"]["credentials"][0]["fills"] == ["email"]
    missing = await client.get(f"/api/v1/catalog/issuers/{offered['slug']}/offers/employment")
    assert missing.status_code == 404

    # Start: the portal keeps the secret.
    started = (
        await client.post(
            "/api/v1/applications", json={"issuer": offered["slug"], "type": "membership"}
        )
    ).json()
    base = f"/api/v1/applications/{started['id']}"
    secret = {"X-Application-Secret": started["secret"]}
    assert (await client.get(base, headers={"X-Application-Secret": "nope"})).status_code == 404
    assert (await client.get(base, headers=secret)).json()["status"] == "open"
    early = await client.put(f"{base}/answers", json={"answers": {}}, headers=secret)
    assert early.status_code == 409 and early.json()["detail"] == "application_not_paired"

    # QR 1: the wallet pairs with the key it keeps for this issuer.
    holder = FakeWallet()
    pairing = await _ask(client, base, secret, "pair")
    assert pairing["purpose"] == "pair" and pairing["response_type"] == "id_token"
    assert pairing["client_id"] == PORTAL and pairing["issuer"]["did"] == club.did
    answered = await client.post(
        urlparse(pairing["response_uri"]).path, data={"id_token": holder.id_token(pairing)}
    )
    assert answered.status_code == 204, answered.text
    state = (await client.get(base, headers=secret)).json()
    assert state["status"] == "paired" and state["holder_did"] == holder.did
    assert state["wallet"] == {**state["wallet"], "purpose": "pair", "answered": True}
    again = await client.post(urlparse(pairing["response_uri"]).path, data={"id_token": "x"})
    assert again.status_code == 409

    # Present: a verified email, which fills the email field.
    asked = await _ask(client, base, secret, "present")
    assert asked["response_type"] == "vp_token"
    assert asked["dcql_query"]["credential_sets"][0]["required"] is True
    email = sd_jwt(
        mailer,
        FakeWallet(),
        {"email": "lucia@example.org"},
        vct="https://almena.id/credentials/verified_email/v1",
        nonce=asked["nonce"],
        aud=asked["client_id"],
    )
    presented = await client.post(
        urlparse(asked["response_uri"]).path,
        json={"vp_token": {"verified_email_sd_jwt": [email]}},
    )
    assert presented.status_code == 204, presented.text
    state = (await client.get(base, headers=secret)).json()
    assert state["filled"] == {"email": "lucia@example.org"}
    assert state["presented"][0]["verified"] is True

    # Files: only what the form accepts.
    wrong = await client.post(
        f"{base}/files",
        data={"key": "id_scan"},
        files={"file": ("id.png", b"png", "image/png")},
        headers=secret,
    )
    assert wrong.status_code == 422 and wrong.json()["detail"] == "file_type_invalid"
    scan = await client.post(
        f"{base}/files",
        data={"key": "id_scan"},
        files={"file": ("id.pdf", b"%PDF-1.7 scan", "application/pdf")},
        headers=secret,
    )
    assert scan.status_code == 200, scan.text
    assert scan.json()["digest"].startswith("sha256-") and scan.json()["size"] == 13

    # Answers: each checked against its field.
    bad = await client.put(
        f"{base}/answers",
        json={
            "answers": {
                "given_name": "Lucía",
                "birthdate": "2015-03-14",
                "address": {
                    "street_address": "Calle Mayor 3",
                    "locality": "Sevilla",
                    "postal_code": "41001",
                },
                "nationalities": ["ES", "XX"],
            }
        },
        headers=secret,
    )
    assert bad.status_code == 422
    assert bad.json()["detail"] == {
        "code": "answers_invalid",
        "errors": {"birthdate": "range", "address.country": "required", "nationalities": "value"},
    }
    incomplete = await client.post(f"{base}/wallet", json={"purpose": "submit"}, headers=secret)
    assert incomplete.status_code == 409 and incomplete.json()["detail"] == "answers_incomplete"
    good = await client.put(
        f"{base}/answers",
        json={
            "answers": {
                "given_name": " Lucía ",
                "birthdate": "1990-03-14",
                "address": {
                    "street_address": "Calle Mayor 3",
                    "locality": "Sevilla",
                    "postal_code": "41001",
                    "country": "ES",
                },
                "nationalities": ["ES"],
            }
        },
        headers=secret,
    )
    assert good.status_code == 200, good.text
    assert good.json()["answers"]["given_name"] == "Lucía"

    # QR 2: the wallet reads what it signs, checks the digest and signs it.
    submit = await _ask(client, base, secret, "submit")
    content = submit["submission"]["content"]
    assert submit["submission"]["digest"] == hashlib.sha256(webvh.jcs(content)).hexdigest()
    shown = {answer["key"]: answer for answer in content["answers"]}
    assert shown["email"]["verified"] is True
    assert shown["nationalities"]["text"] == {"en": "Spain", "es": "España"}
    assert shown["id_scan"]["text"]["en"] == "id.pdf"
    response_path = urlparse(submit["response_uri"]).path
    stranger = FakeWallet()
    for signature, problem in (
        (_sign_submission(stranger, submit, iss=stranger.did), "not_the_holder"),
        (_sign_submission(holder, submit, digest="0" * 64), "digest_mismatch"),
        (_sign_submission(holder, submit, nonce="old"), "invalid_signature"),
    ):
        refused = await client.post(response_path, json={"signature": signature})
        assert refused.status_code == 400 and refused.json()["detail"] == problem
    signed = await client.post(response_path, json={"signature": _sign_submission(holder, submit)})
    assert signed.status_code == 204, signed.text
    state = (await client.get(base, headers=secret)).json()
    assert state["status"] == "submitted" and state["submitted_at"]
    closed = await client.put(f"{base}/answers", json={"answers": {}}, headers=secret)
    assert closed.status_code == 409 and closed.json()["detail"] == "application_closed"

    # The issuer's tenant reads it, checks the signature, and decides.
    inbox = f"/api/v1/tenants/{tenant}/applications"
    listed = (await client.get(inbox, headers=headers)).json()
    assert [(a["slug"], a["status"]) for a in listed] == [(started["slug"], "submitted")]
    received = (await client.get(f"{inbox}/{started['id']}", headers=headers)).json()
    assert received["signature_valid"] is True and received["holder_did"] == holder.did
    assert received["content"] == content
    download = await client.get(f"{inbox}/{started['id']}/files/id_scan", headers=headers)
    assert download.content == b"%PDF-1.7 scan"
    assert download.headers["content-type"] == "application/pdf"
    decided = await client.post(
        f"{inbox}/{started['id']}/decision",
        json={"decision": "accepted", "note": "Welcome"},
        headers=headers,
    )
    assert decided.json()["status"] == "accepted"
    twice = await client.post(
        f"{inbox}/{started['id']}/decision", json={"decision": "rejected"}, headers=headers
    )
    assert twice.status_code == 409
    assert (await client.get(base, headers=secret)).json()["decision_note"] == "Welcome"

    # Nobody else's inbox.
    from tests.test_directory import _sign_in

    eve, other = await _sign_in(client, outbox, "eve@example.org")
    assert (await client.get(f"/api/v1/tenants/{other}/applications", headers=eve)).json() == []
    assert (await client.get(f"{inbox}/{started['id']}", headers=eve)).status_code == 404


async def test_an_offer_needs_a_form_of_the_tenants(client: AsyncClient, outbox: Outbox) -> None:
    _, _, headers, tenant = await _offer(client, outbox)
    issuer_id = (await client.get(f"/api/v1/tenants/{tenant}/issuers", headers=headers)).json()[
        "items"
    ][0]["id"]
    url = f"/api/v1/tenants/{tenant}/issuers/{issuer_id}/credential-types"
    for body in (
        {"types": ["membership"], "forms": {"employment": "00000000-0000-0000-0000-000000000000"}},
        {"types": ["membership"], "forms": {"membership": "00000000-0000-0000-0000-000000000000"}},
    ):
        refused = await client.put(url, json=body, headers=headers)
        assert refused.status_code == 422 and refused.json()["detail"] == "request_form_invalid"
