from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.models import Session
from tests.conftest import Outbox
from tests.test_account import _sign_in

TOKENS = "/api/v1/auth/me/tokens"


async def test_a_token_acts_as_its_account_until_revoked(
    client: AsyncClient, outbox: Outbox
) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    made = await client.post(TOKENS, json={"name": "  CI  ", "expires_in_days": 30}, headers=ada)
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["name"] == "CI"
    assert body["token"].startswith("almena_")
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(days=29) < expires - datetime.now(UTC) <= timedelta(days=30)

    token = {"Authorization": f"Bearer {body['token']}"}
    me = await client.get("/api/v1/auth/me", headers=token)
    assert me.json()["email"] == "ada@example.org"

    # Listed without its secret; a sign-in is not a token.
    listed = (await client.get(TOKENS, headers=ada)).json()["items"]
    assert [t["name"] for t in listed] == ["CI"]
    assert "token" not in listed[0]

    gone = await client.delete(f"{TOKENS}/{body['id']}", headers=ada)
    assert gone.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=token)).status_code == 401
    assert (await client.get(TOKENS, headers=ada)).json()["items"] == []


async def test_a_token_is_never_idle(client: AsyncClient, outbox: Outbox, db: AsyncSession) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    token = (await client.post(TOKENS, json={"name": "CI"}, headers=ada)).json()
    long_ago = datetime.now(UTC) - timedelta(days=10)
    await db.execute(update(Session).values(last_seen_at=long_ago))
    await db.commit()
    headers = {"Authorization": f"Bearer {token['token']}"}
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    # The sign-in has gone idle; and a later sign-in's sweep leaves the token.
    assert (await client.get("/api/v1/auth/me", headers=ada)).status_code == 401
    await _sign_in(client, outbox, "ada@example.org")
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200


async def test_an_expired_token_is_refused(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    token = (await client.post(TOKENS, json={"name": "CI"}, headers=ada)).json()
    await db.execute(
        update(Session)
        .where(Session.name.is_not(None))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db.commit()
    headers = {"Authorization": f"Bearer {token['token']}"}
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    assert (await client.get(TOKENS, headers=ada)).json()["items"] == []


async def test_what_a_token_may_be_made_with(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    for body, code in [
        ({"name": "   "}, "name_required"),
        ({"name": "x" * 101}, None),
        ({"name": "CI", "expires_in_days": 0}, None),
        ({"name": "CI", "expires_in_days": 366}, None),
        ({}, None),
    ]:
        response = await client.post(TOKENS, json=body, headers=ada)
        assert response.status_code == 422, body
        if code:
            assert response.json()["detail"] == code
    assert (await client.post(TOKENS, json={"name": "CI"})).status_code == 401


async def test_a_token_cannot_make_another(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    token = (await client.post(TOKENS, json={"name": "CI"}, headers=ada)).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    refused = await client.post(TOKENS, json={"name": "more"}, headers=headers)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "sign_in_required"


async def test_tokens_are_the_accounts_own(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    bob = await _sign_in(client, outbox, "bob@example.org")
    token = (await client.post(TOKENS, json={"name": "CI"}, headers=ada)).json()
    assert (await client.get(TOKENS, headers=bob)).json()["items"] == []
    refused = await client.delete(f"{TOKENS}/{token['id']}", headers=bob)
    assert refused.status_code == 404
    assert refused.json()["detail"] == "token_not_found"
    # A sign-in is not a token, even for its own account.
    sign_in = await db.scalar(select(Session).where(Session.name.is_(None)).limit(1))
    assert sign_in is not None
    response = await client.delete(f"{TOKENS}/{sign_in.id}", headers=ada)
    assert response.status_code == 404


async def test_an_account_holds_so_many_tokens(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    for n in range(50):
        made = await client.post(TOKENS, json={"name": f"t{n}"}, headers=ada)
        assert made.status_code == 201
    refused = await client.post(TOKENS, json={"name": "one more"}, headers=ada)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "too_many_tokens"
