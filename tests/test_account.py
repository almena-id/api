import uuid
from urllib.parse import parse_qs, urlparse

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.config import Settings
from registry_api.models import Tenant, TenantMember, User
from tests.conftest import Outbox
from tests.test_oauth import _google, _provider_answers

Headers = dict[str, str]


async def _sign_in(client: AsyncClient, outbox: Outbox, email: str) -> Headers:
    await client.post("/api/v1/auth/code", json={"email": email})
    response = await client.post(
        "/api/v1/auth/verify", json={"email": email, "code": outbox.last_code()}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def _link_email(
    client: AsyncClient, outbox: Outbox, headers: Headers, email: str
) -> dict[str, str | None]:
    await client.post("/api/v1/auth/code", json={"email": email})
    response = await client.post(
        "/api/v1/auth/me/email",
        json={"email": email, "code": outbox.last_code()},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, str | None] = response.json()
    return body


async def _link_google(client: AsyncClient, headers: Headers, **claims: str) -> dict[str, object]:
    start = await client.post("/api/v1/auth/me/accounts/google/start", headers=headers)
    assert start.status_code == 200, start.text
    url = start.json()["authorization_url"]
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    _provider_answers(_google(lambda: query["nonce"], **claims))
    response = await client.post(
        "/api/v1/auth/me/accounts/google/callback",
        json={"code": "the-code", "state": query["state"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def test_ways_in_are_linked_and_unlinked(
    client: AsyncClient, outbox: Outbox, settings: Settings
) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    ways = (await client.get("/api/v1/auth/me/ways-in", headers=ada)).json()
    assert ways == {"email": "ada@example.org", "accounts": [], "removable": False}

    assert (await _link_google(client, ada, email="other@example.org"))["status"] == "linked"
    ways = (await client.get("/api/v1/auth/me/ways-in", headers=ada)).json()
    accounts = ways["accounts"]
    assert [(a["provider"], a["email"]) for a in accounts] == [("google", "other@example.org")]
    assert ways["removable"] is True

    # The email can go while Google remains; then Google is the last way in.
    assert (await client.delete("/api/v1/auth/me/email", headers=ada)).status_code == 204
    me = (await client.get("/api/v1/auth/me", headers=ada)).json()
    assert me["email"] is None
    ways = (await client.get("/api/v1/auth/me/ways-in", headers=ada)).json()
    assert ways["removable"] is False
    last = await client.delete(f"/api/v1/auth/me/accounts/{accounts[0]['id']}", headers=ada)
    assert last.status_code == 409 and last.json()["detail"] == "last_way_in"


async def test_linking_an_email_replaces_it_and_accepts_invitations(
    client: AsyncClient, outbox: Outbox
) -> None:
    bob = await _sign_in(client, outbox, "bob@example.org")
    tenant = (await client.get("/api/v1/tenants", headers=bob)).json()[0]["id"]
    invited = await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "ada@example.org", "role": "member"},
        headers=bob,
    )
    assert invited.status_code == 201, invited.text

    # Ada's account has an address of its own and links the invited one.
    ada = await _sign_in(client, outbox, "ada@old.example.org")
    assert (await _link_email(client, outbox, ada, "ada@example.org"))["status"] == "linked"
    assert (await client.get("/api/v1/auth/me", headers=ada)).json()["email"] == "ada@example.org"
    tenants = (await client.get("/api/v1/tenants", headers=ada)).json()
    assert tenant in [t["id"] for t in tenants]


async def test_a_wrong_code_links_nothing(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    await client.post("/api/v1/auth/code", json={"email": "new@example.org"})
    wrong = "000000" if outbox.last_code() != "000000" else "111111"
    response = await client.post(
        "/api/v1/auth/me/email", json={"email": "new@example.org", "code": wrong}, headers=ada
    )
    assert response.status_code == 401


async def test_a_way_in_of_a_busy_account_stays_there(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    await _sign_in(client, outbox, "owner@example.org")
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = (await client.get("/api/v1/tenants", headers=ada)).json()[0]["id"]
    created = await client.post(
        f"/api/v1/tenants/{tenant}/issuers", json={"name": "Diplomas"}, headers=ada
    )
    assert created.status_code == 201, created.text

    result = await _link_email(client, outbox, ada, "owner@example.org")
    assert result == {"status": "taken", "move_ticket": None}
    assert (await client.get("/api/v1/auth/me", headers=ada)).json()["email"] == "ada@example.org"


async def test_an_empty_account_moves_to_the_owner(
    client: AsyncClient, outbox: Outbox, settings: Settings, db: AsyncSession
) -> None:
    owner = await _sign_in(client, outbox, "owner@example.org")
    owner_id = (await client.get("/api/v1/auth/me", headers=owner)).json()["id"]

    # A fresh account that has only linked a provider account.
    ada = await _sign_in(client, outbox, "ada@example.org")
    ada_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=ada)).json()["id"])
    await _link_google(client, ada)

    result = await _link_email(client, outbox, ada, "owner@example.org")
    assert result["status"] == "taken" and result["move_ticket"]

    moved = await client.post(
        "/api/v1/auth/me/move", json={"ticket": result["move_ticket"]}, headers=ada
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["user"]["id"] == owner_id
    after = {"Authorization": f"Bearer {moved.json()['token']}"}
    ways = (await client.get("/api/v1/auth/me/ways-in", headers=after)).json()
    assert ways["email"] == "owner@example.org"
    assert [a["provider"] for a in ways["accounts"]] == ["google"]

    # The empty account and its tenant are gone, and so is its session.
    assert await db.scalar(select(User).where(User.email == "ada@example.org")) is None
    assert await db.scalar(select(TenantMember).where(TenantMember.user_id == ada_id)) is None
    names = list(await db.scalars(select(Tenant.name).where(Tenant.root.is_(False))))
    assert names == ["Tenant of owner@example.org"]
    assert (await client.get("/api/v1/auth/me", headers=ada)).status_code == 401

    # A ticket is good once.
    again = await client.post(
        "/api/v1/auth/me/move", json={"ticket": result["move_ticket"]}, headers=after
    )
    assert again.status_code == 400


async def test_a_ticket_is_only_for_the_account_it_was_given_to(
    client: AsyncClient, outbox: Outbox
) -> None:
    await _sign_in(client, outbox, "owner@example.org")
    ada = await _sign_in(client, outbox, "ada@example.org")
    result = await _link_email(client, outbox, ada, "owner@example.org")
    bob = await _sign_in(client, outbox, "bob@example.org")
    response = await client.post(
        "/api/v1/auth/me/move", json={"ticket": result["move_ticket"]}, headers=bob
    )
    assert response.status_code == 400


async def test_a_linking_flow_does_not_sign_in(
    client: AsyncClient, outbox: Outbox, settings: Settings
) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    start = await client.post("/api/v1/auth/me/accounts/google/start", headers=ada)
    state = start.json()["state"]
    response = await client.post(
        "/api/v1/auth/oauth/google/callback", json={"code": "the-code", "state": state}
    )
    assert response.status_code == 400 and response.json()["detail"] == "invalid_state"


async def test_a_provider_account_of_another_stays_there(
    client: AsyncClient, outbox: Outbox, settings: Settings
) -> None:
    # Google's g-123 signs up an account of its own.
    first = await _sign_in(client, outbox, "ada@example.org")
    await _link_google(client, first)
    bob = await _sign_in(client, outbox, "bob@example.org")
    result = await _link_google(client, bob)
    assert result["status"] == "taken" and result["move_ticket"]
