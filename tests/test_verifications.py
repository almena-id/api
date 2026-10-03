import json
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from httpx import AsyncClient

from registry_api.broker.memory import MemoryBroker
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.signing import publish, ready, sign
from tests.test_directory import _sign_in
from tests.test_presentations import _issuer, sd_jwt


async def _verifier_and_form(
    client: AsyncClient, outbox: Outbox
) -> tuple[dict[str, str], str, dict[str, Any], str]:
    """A tenant with a published verifier and a form asking for a membership."""
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}"
    verifier = (
        await client.post(f"{base}/verifiers", json={"name": "Door"}, headers=headers)
    ).json()
    form = await client.post(
        f"{base}/forms",
        json={
            "name": {"en": "Members' entrance"},
            "fields": [{"ref": "given_name"}],
            "credentials": [{"type": "membership", "claims": ["given_name", "member_number"]}],
        },
        headers=headers,
    )
    assert form.status_code == 201, form.text
    url = f"{base}/verifiers/{verifier['id']}/verifications"
    # Unpublished, it cannot ask: the wallet would be shown a DID that does not resolve.
    early = await client.post(url, json={"form_id": form.json()["id"]}, headers=headers)
    assert early.status_code == 409 and early.json()["detail"] == "verifier_unpublished"
    wallet = await ready(client, headers, tenant)
    await sign(client, headers, tenant, verifier["identity"]["id"], wallet)
    await publish(client, headers, tenant, "verifiers", verifier["id"], wallet)
    return headers, tenant, verifier, form.json()["id"]


async def _request(client: AsyncClient, deep_link: str) -> dict[str, Any]:
    uri = unquote(parse_qs(urlparse(deep_link).query)["request_uri"][0])
    request: dict[str, Any] = (await client.get(urlparse(uri).path)).json()
    return request


async def test_a_verifier_asks_by_qr_and_sees_the_verdict(
    client: AsyncClient, outbox: Outbox, broker: MemoryBroker
) -> None:
    club = await _issuer(client, outbox, "club@example.org", ["membership"])
    headers, tenant, verifier, form_id = await _verifier_and_form(client, outbox)
    base = f"/api/v1/tenants/{tenant}/verifiers/{verifier['id']}"
    made = await client.post(f"{base}/queue", headers=headers)
    assert made.status_code == 201, made.text

    opened = await client.post(f"{base}/verifications", json={"form_id": form_id}, headers=headers)
    assert opened.status_code == 201, opened.text
    verification = opened.json()
    assert verification["status"] == "pending" and verification["result"] is None
    assert verification["verifier"]["name"] == "Door"

    # What the wallet reads: who asks, and the DCQL query of the form.
    request = await _request(client, verification["deep_link"])
    assert request["purpose"] == "verify" and request["response_type"] == "vp_token"
    assert request["client_id"] == "https://registry.almena.id"
    assert request["verifier"] == {"did": verification["verifier"]["did"], "name": "Door"}
    assert request["dcql_query"]["credentials"][0]["id"] == "membership_sd_jwt"
    assert request["credentials"][0]["key"] == "membership"

    holder = FakeWallet()
    presented = sd_jwt(
        club,
        holder,
        {"given_name": "Lucía", "member_number": "0042"},
        nonce=request["nonce"],
        aud=request["client_id"],
    )
    answer = urlparse(request["response_uri"]).path
    answered = await client.post(
        answer, data={"vp_token": json.dumps({"membership_sd_jwt": [presented]})}
    )
    assert answered.status_code == 204, answered.text
    # Once.
    again = await client.post(answer, json={"vp_token": {"membership_sd_jwt": [presented]}})
    assert again.status_code == 409 and again.json()["detail"] == "already_answered"

    seen = (await client.get(f"{base}/verifications/{verification['id']}", headers=headers)).json()
    assert seen["status"] == "answered" and seen["deep_link"] is None
    assert seen["result"]["verified"] is True
    credential = seen["result"]["credentials"][0]
    assert credential["issuer"] == club.did
    assert credential["claims"]["member_number"] == "0042"
    assert credential["fills"] == {"given_name": "Lucía"}

    # The verifier's queue hears of it.
    queue = made.json()["queue"]
    kind, message = broker.queues[queue][-1]
    assert kind == "presentation.verified" and message["verified"] is True
    assert message["verification"]["id"] == verification["id"]


async def test_a_wrong_answer_is_verified_as_wrong(client: AsyncClient, outbox: Outbox) -> None:
    club = await _issuer(client, outbox, "club@example.org", ["membership"])
    headers, tenant, verifier, form_id = await _verifier_and_form(client, outbox)
    base = f"/api/v1/tenants/{tenant}/verifiers/{verifier['id']}/verifications"
    verification = (await client.post(base, json={"form_id": form_id}, headers=headers)).json()
    request = await _request(client, verification["deep_link"])
    answer = urlparse(request["response_uri"]).path
    bad = await client.post(answer, data={"vp_token": "not json"})
    assert bad.status_code == 400 and bad.json()["detail"] == "invalid_vp_token"
    # Bound to another request's nonce.
    replayed = sd_jwt(club, FakeWallet(), nonce="other", aud=request["client_id"])
    await client.post(answer, json={"vp_token": {"membership_sd_jwt": [replayed]}})
    seen = (await client.get(f"{base}/{verification['id']}", headers=headers)).json()
    assert seen["result"]["verified"] is False
    assert "nonce_mismatch" in seen["result"]["credentials"][0]["problems"]


async def test_only_forms_with_credentials_and_the_tenants_own(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant, verifier, _ = await _verifier_and_form(client, outbox)
    base = f"/api/v1/tenants/{tenant}"
    plain = (
        await client.post(
            f"{base}/forms",
            json={"name": {"en": "Plain"}, "fields": [{"ref": "given_name"}]},
            headers=headers,
        )
    ).json()
    url = f"{base}/verifiers/{verifier['id']}/verifications"
    empty = await client.post(url, json={"form_id": plain["id"]}, headers=headers)
    assert empty.status_code == 409 and empty.json()["detail"] == "nothing_to_present"
    eve, eves = await _sign_in(client, outbox, "eve@example.org")
    theirs = await client.post(
        f"/api/v1/tenants/{eves}/verifiers/{verifier['id']}/verifications",
        json={"form_id": plain["id"]},
        headers=eve,
    )
    assert theirs.status_code == 404
