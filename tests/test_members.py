from httpx import AsyncClient

from tests.conftest import Outbox


async def _sign_in(client: AsyncClient, outbox: Outbox, email: str) -> dict[str, str]:
    await client.post("/api/v1/auth/code", json={"email": email})
    response = await client.post(
        "/api/v1/auth/verify", json={"email": email, "code": outbox.last_code()}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def _tenants(client: AsyncClient, headers: dict[str, str]) -> list[dict[str, str]]:
    tenants: list[dict[str, str]] = (await client.get("/api/v1/tenants", headers=headers)).json()
    return tenants


async def test_the_creator_is_admin(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = (await _tenants(client, ada))[0]
    assert tenant["role"] == "admin"
    members = (await client.get(f"/api/v1/tenants/{tenant['id']}/members", headers=ada)).json()
    assert [(m["email"], m["role"], m["status"]) for m in members] == [
        ("ada@example.org", "admin", "member")
    ]


async def test_an_invitation_becomes_membership_on_sign_in(
    client: AsyncClient, outbox: Outbox
) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = (await _tenants(client, ada))[0]["id"]
    base = f"/api/v1/tenants/{tenant}"

    response = await client.post(
        f"{base}/invitations",
        json={"email": "Bob@Example.org", "role": "member", "locale": "es"},
        headers=ada,
    )
    assert response.status_code == 201, response.text
    to, subject, body = outbox.sent[-1]
    assert to == "bob@example.org"
    assert subject.startswith("Te han invitado")
    assert "ada@example.org" in body and "/login" in body

    listed = (await client.get(f"{base}/members", headers=ada)).json()
    assert [(m["email"], m["status"]) for m in listed] == [
        ("ada@example.org", "member"),
        ("bob@example.org", "invited"),
    ]

    # Bob is new: he joins Ada's tenant, and gets no empty one of his own.
    bob = await _sign_in(client, outbox, "bob@example.org")
    bob_tenants = await _tenants(client, bob)
    assert [(t["id"], t["role"]) for t in bob_tenants] == [(tenant, "member")]
    listed = (await client.get(f"{base}/members", headers=ada)).json()
    assert [(m["email"], m["status"]) for m in listed][1] == ("bob@example.org", "member")


async def test_an_existing_account_joins_too(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    await _sign_in(client, outbox, "bob@example.org")
    tenant = (await _tenants(client, ada))[0]["id"]
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "admin"},
        headers=ada,
    )
    bob = await _sign_in(client, outbox, "bob@example.org")
    roles = {t["id"]: t["role"] for t in await _tenants(client, bob)}
    assert len(roles) == 2 and roles[tenant] == "admin"


async def test_inviting_again_updates_the_role(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = (await _tenants(client, ada))[0]["id"]
    for role in ["member", "admin"]:
        await client.post(
            f"/api/v1/tenants/{tenant}/invitations",
            json={"email": "bob@example.org", "role": role},
            headers=ada,
        )
    listed = (await client.get(f"/api/v1/tenants/{tenant}/members", headers=ada)).json()
    assert [(m["email"], m["role"]) for m in listed][1:] == [("bob@example.org", "admin")]


async def test_a_member_cannot_invite(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = (await _tenants(client, ada))[0]["id"]
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "member"},
        headers=ada,
    )
    bob = await _sign_in(client, outbox, "bob@example.org")
    response = await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "carol@example.org", "role": "member"},
        headers=bob,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "not_admin"
    # But a member does see who belongs.
    assert (await client.get(f"/api/v1/tenants/{tenant}/members", headers=bob)).status_code == 200


async def test_a_member_is_not_invited_again(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = (await _tenants(client, ada))[0]["id"]
    response = await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "ada@example.org", "role": "member"},
        headers=ada,
    )
    assert response.status_code == 409


async def test_roles_are_admin_or_member(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = (await _tenants(client, ada))[0]["id"]
    response = await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "owner"},
        headers=ada,
    )
    assert response.status_code == 422


async def test_outsiders_see_nothing(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    eve = await _sign_in(client, outbox, "eve@example.org")
    tenant = (await _tenants(client, ada))[0]["id"]
    assert (await client.get(f"/api/v1/tenants/{tenant}/members", headers=eve)).status_code == 404
    response = await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "eve@example.org", "role": "admin"},
        headers=eve,
    )
    assert response.status_code == 404
