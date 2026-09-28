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
    assert picked.json()["mediator"] == {"id": mediator["id"], "name": "Relay"}

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
