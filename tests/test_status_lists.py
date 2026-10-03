import base64
import hashlib
import time
from typing import Any
from urllib.parse import urlparse

import jwt
from httpx import AsyncClient

from registry_api import status_lists
from registry_api.main import app
from registry_api.presentations import get_status_fetch
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.test_issuance import (
    _accepted,
    answer_jws,
    issued,
    issuer_id_of,
    read_sign_request,
    sign_status_list,
)


def test_statuses_pack_as_the_draft_says() -> None:
    data = status_lists.empty(16)
    assert len(data) == 4
    data = status_lists.with_value(data, 0, 1)
    data = status_lists.with_value(data, 5, 2)
    # Index 0 in the lowest bits of byte 0; index 5 in bits 2-3 of byte 1.
    assert data[0] == 0b01 and data[1] == 0b1000
    assert [status_lists.value_at(data, i) for i in (0, 1, 5)] == [1, 0, 2]
    assert status_lists.decode(status_lists.encode(data)) == data
    assert status_lists.counts(data) == {"revoked": 1, "suspended": 1}
    assert status_lists.value_at(status_lists.with_value(data, 5, 0), 5) == 0


async def _change(
    client: AsyncClient, headers: dict[str, str], tenant: str, application_id: str, to: str
) -> Any:
    return await client.post(
        f"/api/v1/tenants/{tenant}/applications/{application_id}/credential-status",
        json={"status": to, "locale": "en"},
        headers=headers,
    )


async def _verify(
    client: AsyncClient, headers: dict[str, str], tenant: str, holder: FakeWallet, credential: str
) -> dict[str, Any]:
    async def fetch(url: str) -> str:
        found = await client.get(urlparse(url).path)
        found.raise_for_status()
        return found.text

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
    result: dict[str, Any] = (
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
    return result


async def test_the_issuers_signer_suspends_reinstates_and_revokes(
    client: AsyncClient, outbox: Outbox
) -> None:
    club, headers, tenant, application_id, secret, holder, credential = await issued(client, outbox)
    inbox = f"/api/v1/tenants/{tenant}/applications/{application_id}"
    received = (await client.get(inbox, headers=headers)).json()
    assert received["credential_status"] == "valid"
    assert received["credential_statuses"] == ["suspended", "revoked"]
    assert received["status_list"]["uri"].startswith("https://api.almena.id/status-lists/stl_")
    assert (await _verify(client, headers, tenant, holder, credential))["verified"] is True

    # Suspended once the signer's wallet signs the list that says so.
    asked = await _change(client, headers, tenant, application_id, "suspended")
    assert asked.status_code == 200, asked.text
    request = await read_sign_request(client, asked.json())
    sign = request["sign"]
    assert sign["kind"] == "status_list" and sign["did"] == club.did
    index = sign["status_change"]["index"]
    assert sign["status_change"]["status"] == "suspended"
    assert sign["status_change"]["holder"] == holder.did
    lst = status_lists.decode(sign["document"]["payload"]["status_list"]["lst"])
    assert status_lists.value_at(lst, index) == 2
    # Until then, nothing changed.
    assert (await client.get(inbox, headers=headers)).json()["credential_status"] == "valid"
    forged = await answer_jws(client, request, FakeWallet())
    assert forged.status_code == 400
    assert (await answer_jws(client, request, club.wallet)).status_code == 204
    suspended = (await client.get(inbox, headers=headers)).json()
    assert suspended["credential_status"] == "suspended"
    assert suspended["credential_statuses"] == ["valid", "revoked"]
    holder_view = (
        await client.get(f"/api/v1/applications/{application_id}", headers=secret)
    ).json()
    assert holder_view["credential_status"] == "suspended"
    result = await _verify(client, headers, tenant, holder, credential)
    assert result["credentials"][0]["problems"] == ["suspended"]

    # The public list is the token as signed.
    public = await client.get(urlparse(sign["document"]["payload"]["sub"]).path)
    assert public.headers["content-type"] == "application/statuslist+jwt"
    assert jwt.get_unverified_header(public.text)["typ"] == "statuslist+jwt"

    same = await _change(client, headers, tenant, application_id, "suspended")
    assert same.status_code == 409 and same.json()["detail"] == "status_unchanged"

    # Reinstated, it holds again.
    asked = await _change(client, headers, tenant, application_id, "valid")
    reinstated = await answer_jws(
        client, await read_sign_request(client, asked.json()), club.wallet
    )
    assert reinstated.status_code == 204
    assert (await _verify(client, headers, tenant, holder, credential))["verified"] is True

    # Revoked, for good.
    asked = await _change(client, headers, tenant, application_id, "revoked")
    revoked = await answer_jws(client, await read_sign_request(client, asked.json()), club.wallet)
    assert revoked.status_code == 204
    result = await _verify(client, headers, tenant, holder, credential)
    assert result["credentials"][0]["problems"] == ["revoked"]
    final = await _change(client, headers, tenant, application_id, "valid")
    assert final.status_code == 409 and final.json()["detail"] == "credential_revoked"

    issuer_id = await issuer_id_of(client, headers, tenant)
    lists = (
        await client.get(
            f"/api/v1/tenants/{tenant}/issuers/{issuer_id}/status-lists", headers=headers
        )
    ).json()
    assert lists["can_sign"] is True
    (only,) = lists["items"]
    assert only["used"] == 1 and only["revoked"] == 1 and only["suspended"] == 0
    assert only["revision"] == 4 and only["needs_signing"] is False


async def test_a_list_signed_meanwhile_refuses_an_older_change(
    client: AsyncClient, outbox: Outbox
) -> None:
    club, headers, tenant, application_id, _, _, _ = await issued(client, outbox)
    first = await _change(client, headers, tenant, application_id, "suspended")
    second = await _change(client, headers, tenant, application_id, "revoked")
    first_request = await read_sign_request(client, first.json())
    second_request = await read_sign_request(client, second.json())
    assert (await answer_jws(client, second_request, club.wallet)).status_code == 204
    stale = await answer_jws(client, first_request, club.wallet)
    assert stale.status_code == 409 and stale.json()["detail"] == "status_list_changed"
    inbox = f"/api/v1/tenants/{tenant}/applications/{application_id}"
    revoked = (await client.get(inbox, headers=headers)).json()
    assert revoked["credential_status"] == "revoked"
    assert revoked["credential_statuses"] == []


async def test_what_a_status_change_and_a_signature_ask_for(
    client: AsyncClient, outbox: Outbox
) -> None:
    club, headers, tenant, application_id, _, _ = await _accepted(client, outbox)
    issuer_id = await issuer_id_of(client, headers, tenant)
    lists = f"/api/v1/tenants/{tenant}/issuers/{issuer_id}/status-lists"
    assert (await client.get(lists, headers=headers)).json()["items"] == []

    # Only issued credentials change status.
    early = await _change(client, headers, tenant, application_id, "revoked")
    assert early.status_code == 409 and early.json()["detail"] == "not_issued"
    bad = await _change(client, headers, tenant, application_id, "expired")
    assert bad.status_code == 422

    # Signed, a list is public; signed with a key in force, it is up to date.
    request = await sign_status_list(client, headers, tenant, club.wallet)
    path = urlparse(request["sign"]["document"]["payload"]["sub"]).path
    assert (await client.get(path)).status_code == 200
    again = await client.post(f"{lists}/sign", json={}, headers=headers)
    assert again.status_code == 409 and again.json()["detail"] == "up_to_date"
    assert (await client.get("/status-lists/stl_nothing")).status_code == 404
    unknown = await client.post(
        f"{lists}/sign",
        json={"status_list_id": "00000000-0000-0000-0000-000000000000"},
        headers=headers,
    )
    assert unknown.status_code == 404 and unknown.json()["detail"] == "status_list_not_found"

    # Only the issuer's signer asks.
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "admin"},
        headers=headers,
    )
    from tests.test_directory import _sign_in

    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    assert (await client.get(lists, headers=bob)).json()["can_sign"] is False
    refused = await client.post(f"{lists}/sign", json={}, headers=bob)
    assert refused.status_code == 403 and refused.json()["detail"] == "not_the_issuers_signer"


async def test_only_the_issuers_signer_changes_a_status(
    client: AsyncClient, outbox: Outbox
) -> None:
    _, headers, tenant, application_id, _, _, _ = await issued(client, outbox)
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "admin"},
        headers=headers,
    )
    from tests.test_directory import _sign_in

    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    refused = await _change(client, bob, tenant, application_id, "revoked")
    assert refused.status_code == 403 and refused.json()["detail"] == "not_the_issuers_signer"
