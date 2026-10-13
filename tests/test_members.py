import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.models import UserIdentity
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


async def test_the_list_says_who_has_a_wallet(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = (await _tenants(client, ada))[0]["id"]
    base = f"/api/v1/tenants/{tenant}"
    invite = {"email": "bob@example.org", "role": "member"}
    await client.post(f"{base}/invitations", json=invite, headers=ada)

    listed = (await client.get(f"{base}/members", headers=ada)).json()
    assert [(m["email"], m["wallet"]) for m in listed] == [
        ("ada@example.org", False),
        # Somebody invited has no account yet: nothing to say.
        ("bob@example.org", None),
    ]

    ada_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=ada)).json()["id"])
    db.add(UserIdentity(user_id=ada_id, provider="almena", subject="did:key:z6Mk"))
    await db.commit()
    listed = (await client.get(f"{base}/members", headers=ada)).json()
    assert listed[0]["wallet"] is True


async def _team(client: AsyncClient, outbox: Outbox) -> tuple[dict[str, str], dict[str, str], str]:
    """Ada's tenant, with Bob in it as a member: their headers and its id."""
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = (await _tenants(client, ada))[0]["id"]
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "member"},
        headers=ada,
    )
    bob = await _sign_in(client, outbox, "bob@example.org")
    return ada, bob, tenant


async def _id_of(client: AsyncClient, headers: dict[str, str], tenant: str, email: str) -> str:
    members = (await client.get(f"/api/v1/tenants/{tenant}/members", headers=headers)).json()
    return str(next(m["user_id"] for m in members if m["email"] == email))


async def test_an_admin_changes_a_role(client: AsyncClient, outbox: Outbox) -> None:
    ada, bob, tenant = await _team(client, outbox)
    base = f"/api/v1/tenants/{tenant}/members"
    bob_id = await _id_of(client, ada, tenant, "bob@example.org")

    denied = await client.patch(f"{base}/{bob_id}", json={"role": "admin"}, headers=bob)
    assert denied.status_code == 403
    made = await client.patch(f"{base}/{bob_id}", json={"role": "admin"}, headers=ada)
    assert made.status_code == 200, made.text
    assert made.json()["role"] == "admin"
    assert (await _tenants(client, bob))[0]["role"] == "admin"
    missing = await client.patch(f"{base}/{uuid.uuid4()}", json={"role": "admin"}, headers=ada)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "member_not_found"


async def test_a_tenant_keeps_an_admin(client: AsyncClient, outbox: Outbox) -> None:
    ada, _, tenant = await _team(client, outbox)
    base = f"/api/v1/tenants/{tenant}/members"
    ada_id = await _id_of(client, ada, tenant, "ada@example.org")
    demoted = await client.patch(f"{base}/{ada_id}", json={"role": "member"}, headers=ada)
    assert demoted.status_code == 409
    assert demoted.json()["detail"] == "last_admin"
    left = await client.delete(f"{base}/{ada_id}", headers=ada)
    assert left.status_code == 409
    assert left.json()["detail"] == "last_admin"


async def test_an_admin_removes_a_member(client: AsyncClient, outbox: Outbox) -> None:
    ada, bob, tenant = await _team(client, outbox)
    base = f"/api/v1/tenants/{tenant}/members"
    bob_id = await _id_of(client, ada, tenant, "bob@example.org")
    ada_id = await _id_of(client, ada, tenant, "ada@example.org")

    # A member removes nobody but themselves.
    assert (await client.delete(f"{base}/{ada_id}", headers=bob)).status_code == 403
    assert (await client.delete(f"{base}/{bob_id}", headers=ada)).status_code == 204
    emails = [m["email"] for m in (await client.get(base, headers=ada)).json()]
    assert emails == ["ada@example.org"]
    # Bob was only in Ada's: he is given one of his own, which he runs.
    mine = await _tenants(client, bob)
    assert len(mine) == 1 and mine[0]["id"] != tenant and mine[0]["role"] == "admin"
    assert mine[0]["name"] == "Tenant of bob@example.org"


async def test_a_member_leaves(client: AsyncClient, outbox: Outbox) -> None:
    ada, bob, tenant = await _team(client, outbox)
    bob_id = await _id_of(client, ada, tenant, "bob@example.org")
    left = await client.delete(f"/api/v1/tenants/{tenant}/members/{bob_id}", headers=bob)
    assert left.status_code == 204
    assert tenant not in [t["id"] for t in await _tenants(client, bob)]
    # Gone, the tenant is out of reach.
    gone = await client.get(f"/api/v1/tenants/{tenant}/members", headers=bob)
    assert gone.status_code == 404


async def test_whoever_signs_stays_until_replaced(client: AsyncClient, outbox: Outbox) -> None:
    ada, _, tenant = await _team(client, outbox)
    base = f"/api/v1/tenants/{tenant}"
    bob_id = await _id_of(client, ada, tenant, "bob@example.org")
    issuer = (await client.post(f"{base}/issuers", json={"name": "Club"}, headers=ada)).json()
    signing = await client.put(
        f"{base}/issuers/{issuer['id']}/signing",
        json={"system": "single_user", "user_id": bob_id},
        headers=ada,
    )
    assert signing.status_code == 200, signing.text
    refused = await client.delete(f"{base}/members/{bob_id}", headers=ada)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "member_signs"

    await client.put(f"{base}/issuers/{issuer['id']}/signing", json={"system": None}, headers=ada)
    assert (await client.delete(f"{base}/members/{bob_id}", headers=ada)).status_code == 204


async def test_an_invitation_is_taken_back(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = (await _tenants(client, ada))[0]["id"]
    base = f"/api/v1/tenants/{tenant}"
    invited = await client.post(
        f"{base}/invitations", json={"email": "bob@example.org", "role": "member"}, headers=ada
    )
    invitation = invited.json()["invitation_id"]
    listed = (await client.get(f"{base}/members", headers=ada)).json()
    assert listed[1]["invitation_id"] == invitation

    assert (await client.delete(f"{base}/invitations/{invitation}", headers=ada)).status_code == 204
    assert len((await client.get(f"{base}/members", headers=ada)).json()) == 1
    again = await client.delete(f"{base}/invitations/{invitation}", headers=ada)
    assert again.status_code == 404
    assert again.json()["detail"] == "invitation_not_found"
    # Bob signs in to a tenant of his own, not Ada's.
    bob = await _sign_in(client, outbox, "bob@example.org")
    assert [t["id"] for t in await _tenants(client, bob)] != [tenant]
