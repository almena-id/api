import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import Outbox
from tests.test_directory import _sign_in


@pytest.mark.parametrize("kind", ["issuers", "verifiers"])
async def test_one_member_signs(client: AsyncClient, outbox: Outbox, kind: str) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    await client.post(
        f"{base}/invitations", json={"email": "bob@example.org", "role": "member"}, headers=ada
    )
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    item = (await client.post(f"{base}/{kind}", json={"name": "Uni"}, headers=ada)).json()
    url = f"{base}/{kind}/{item['id']}/signing"

    assert (await client.get(url, headers=ada)).json() == {"system": None, "signer": None}

    members = (await client.get(f"{base}/members", headers=ada)).json()
    bob_id = next(m["user_id"] for m in members if m["email"] == "bob@example.org")
    body = {"system": "single_user", "user_id": bob_id}
    set_ = await client.put(url, json=body, headers=ada)
    assert set_.status_code == 200, set_.text
    assert set_.json()["system"] == "single_user"
    assert set_.json()["signer"]["email"] == "bob@example.org"
    assert set_.json()["signer"]["member"] is True
    # Any member reads it; only admins set it.
    assert (await client.get(url, headers=bob)).json() == set_.json()
    denied = await client.put(url, json=body, headers=bob)
    assert denied.status_code == 403

    missing = await client.put(url, json={"system": "single_user"}, headers=ada)
    assert missing.status_code == 422 and missing.json()["detail"] == "signer_required"
    eve, _ = await _sign_in(client, outbox, "eve@example.org")
    outsider = {"system": "single_user", "user_id": str(uuid.uuid4())}
    stranger = await client.put(url, json=outsider, headers=ada)
    assert stranger.status_code == 422 and stranger.json()["detail"] == "signer_not_member"
    assert (await client.get(url, headers=eve)).status_code == 404

    cleared = await client.put(url, json={"system": None}, headers=ada)
    assert cleared.json() == {"system": None, "signer": None}


async def test_mediators_have_no_signing(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    body = {"name": "Relay", "url": "https://mediator.example.org"}
    mediator = (await client.post(f"{base}/mediators", json=body, headers=ada)).json()
    response = await client.get(f"{base}/mediators/{mediator['id']}/signing", headers=ada)
    assert response.status_code in (404, 422)
