from httpx import AsyncClient

from tests.conftest import Outbox


async def _sign_in(
    client: AsyncClient, outbox: Outbox, email: str, locale: str = "en"
) -> dict[str, str]:
    await client.post("/api/v1/auth/code", json={"email": email})
    response = await client.post(
        "/api/v1/auth/verify",
        json={"email": email, "code": outbox.last_code(), "locale": locale},
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def test_a_new_account_gets_one_tenant_named_after_it(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers = await _sign_in(client, outbox, "ada@example.org")
    tenants = (await client.get("/api/v1/tenants", headers=headers)).json()
    assert len(tenants) == 1
    assert tenants[0]["name"] == "Tenant of ada@example.org"

    # Signing in again does not add another.
    headers = await _sign_in(client, outbox, "ada@example.org")
    again = (await client.get("/api/v1/tenants", headers=headers)).json()
    assert [t["id"] for t in again] == [tenants[0]["id"]]


async def test_each_account_sees_only_its_own(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    bob = await _sign_in(client, outbox, "bob@example.org")
    ada_tenants = (await client.get("/api/v1/tenants", headers=ada)).json()
    bob_tenants = (await client.get("/api/v1/tenants", headers=bob)).json()
    assert ada_tenants[0]["id"] != bob_tenants[0]["id"]


async def test_tenants_need_a_session(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/tenants")).status_code == 401


async def test_the_tenant_is_named_in_the_portal_language(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers = await _sign_in(client, outbox, "ada@example.org", locale="es")
    tenants = (await client.get("/api/v1/tenants", headers=headers)).json()
    assert tenants[0]["name"] == "Tenant de ada@example.org"
