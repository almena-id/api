from httpx import AsyncClient

from tests.conftest import Outbox


async def _sign_in(client: AsyncClient, outbox: Outbox, email: str) -> dict[str, str]:
    await client.post("/api/v1/auth/code", json={"email": email})
    response = await client.post(
        "/api/v1/auth/verify", json={"email": email, "code": outbox.last_code()}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def _tenant(client: AsyncClient, headers: dict[str, str]) -> str:
    tenant: str = (await client.get("/api/v1/tenants", headers=headers)).json()[0]["id"]
    return tenant


async def test_details_start_without_a_mediator(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    body = (await client.get(f"/api/v1/tenants/{await _tenant(client, ada)}", headers=ada)).json()
    assert body["name"] == "Tenant of ada@example.org"
    assert body["role"] == "admin"
    assert body["mediator"] is None


async def test_the_tenant_picks_one_of_its_mediators(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    url = f"/api/v1/tenants/{await _tenant(client, ada)}"
    body = {"name": "Relay", "url": "https://mediator.example.org"}
    mediator = (await client.post(f"{url}/mediators", json=body, headers=ada)).json()

    picked = await client.patch(url, json={"mediator_id": mediator["id"]}, headers=ada)
    assert picked.status_code == 200, picked.text
    assert picked.json()["mediator"] == {"id": mediator["id"], "name": "Relay", "own": True}

    # The name is left alone when not sent; `null` removes the mediator.
    cleared = await client.patch(url, json={"mediator_id": None}, headers=ada)
    assert cleared.json()["mediator"] is None
    assert cleared.json()["name"] == "Tenant of ada@example.org"


async def test_another_tenants_mediator_cannot_be_picked(
    client: AsyncClient, outbox: Outbox
) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    bob = await _sign_in(client, outbox, "bob@example.org")
    body = {"name": "Bob's", "url": "https://bob.example.org"}
    bobs = (
        await client.post(
            f"/api/v1/tenants/{await _tenant(client, bob)}/mediators", json=body, headers=bob
        )
    ).json()
    response = await client.patch(
        f"/api/v1/tenants/{await _tenant(client, ada)}",
        json={"mediator_id": bobs["id"]},
        headers=ada,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "mediator_not_found"


async def test_rename(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    url = f"/api/v1/tenants/{await _tenant(client, ada)}"
    response = await client.patch(url, json={"name": "  Acme  "}, headers=ada)
    assert response.json()["name"] == "Acme"
    blank = await client.patch(url, json={"name": " "}, headers=ada)
    assert blank.status_code == 422 and blank.json()["detail"] == "name_required"


async def test_only_admins_change_it(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = await _tenant(client, ada)
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "member"},
        headers=ada,
    )
    bob = await _sign_in(client, outbox, "bob@example.org")
    seen = (await client.get(f"/api/v1/tenants/{tenant}", headers=bob)).json()
    assert seen["role"] == "member"
    response = await client.patch(f"/api/v1/tenants/{tenant}", json={"name": "Mine"}, headers=bob)
    assert response.status_code == 403

    eve = await _sign_in(client, outbox, "eve@example.org")
    assert (await client.get(f"/api/v1/tenants/{tenant}", headers=eve)).status_code == 404


async def test_a_tenant_starts_signed_by_any_one_admin(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    url = f"/api/v1/tenants/{await _tenant(client, ada)}"
    assert (await client.get(url, headers=ada)).json()["signing_flow"] == "any_admin"

    kept = await client.patch(url, json={"signing_flow": "any_admin"}, headers=ada)
    assert kept.status_code == 200 and kept.json()["signing_flow"] == "any_admin"
    # Other fields leave it alone, and so does `null`.
    renamed = await client.patch(url, json={"name": "Acme", "signing_flow": None}, headers=ada)
    assert renamed.json()["signing_flow"] == "any_admin"

    unknown = await client.patch(url, json={"signing_flow": "everyone"}, headers=ada)
    assert unknown.status_code == 422


async def test_one_member_can_be_the_tenants_signer(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = await _tenant(client, ada)
    url = f"/api/v1/tenants/{tenant}"
    invite = {"email": "bob@example.org", "role": "member"}
    await client.post(f"{url}/invitations", json=invite, headers=ada)
    bob = await _sign_in(client, outbox, "bob@example.org")
    bob_id = (await client.get("/api/v1/auth/me", headers=bob)).json()["id"]
    eve = await _sign_in(client, outbox, "eve@example.org")
    eve_id = (await client.get("/api/v1/auth/me", headers=eve)).json()["id"]

    nobody = await client.patch(url, json={"signing_flow": "single_user"}, headers=ada)
    assert nobody.status_code == 422 and nobody.json()["detail"] == "signer_required"
    stranger = {"signing_flow": "single_user", "signer_id": eve_id}
    outside = await client.patch(url, json=stranger, headers=ada)
    assert outside.status_code == 422 and outside.json()["detail"] == "signer_not_member"

    chosen = await client.patch(
        url, json={"signing_flow": "single_user", "signer_id": bob_id}, headers=ada
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["signer"]["id"] == bob_id
    # Bob signs, member as he is; Ada, admin, no longer does.
    assert chosen.json()["signs"] is False
    tenants = (await client.get("/api/v1/tenants", headers=bob)).json()
    assert [t["signs"] for t in tenants if t["id"] == tenant] == [True]

    identity = chosen.json()["identity"]["id"]
    sign = f"{url}/identities/{identity}/sign"
    denied = await client.post(sign, json={}, headers=ada)
    assert denied.status_code == 403 and denied.json()["detail"] == "not_a_signer"
    # Bob gets past who may sign; without a wallet there is no key to sign with.
    no_wallet = await client.post(sign, json={}, headers=bob)
    assert no_wallet.status_code == 409 and no_wallet.json()["detail"] == "no_signers"

    back = await client.patch(url, json={"signing_flow": "any_admin"}, headers=ada)
    assert back.json()["signer"] is None and back.json()["signs"] is True
