from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.config import get_settings
from registry_api.mail import Locale, Mailer, get_mailer
from registry_api.main import app
from registry_api.models import Session
from tests.conftest import Outbox


class _Down(Mailer):
    def __init__(self) -> None:
        super().__init__(get_settings())

    async def send_code(self, to: str, code: str, locale: Locale) -> None:
        raise ConnectionRefusedError


async def _ask(client: AsyncClient, email: str = "Ada@Example.org", locale: str = "en") -> None:
    response = await client.post("/api/v1/auth/code", json={"email": email, "locale": locale})
    assert response.status_code == 202, response.text


async def _verify(client: AsyncClient, code: str, email: str = "ada@example.org") -> Response:
    return await client.post("/api/v1/auth/verify", json={"email": email, "code": code})


async def test_code_signs_up_then_in(client: AsyncClient, outbox: Outbox) -> None:
    await _ask(client)
    assert outbox.sent[-1][0] == "ada@example.org"
    first = await _verify(client, outbox.last_code())
    assert first.status_code == 200
    user = first.json()["user"]

    await _ask(client, "ada@example.org")
    second = await _verify(client, outbox.last_code())
    assert second.json()["user"]["id"] == user["id"]

    token = second.json()["token"]
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["email"] == "ada@example.org"


async def test_the_email_speaks_the_portal_language(client: AsyncClient, outbox: Outbox) -> None:
    await _ask(client, locale="es")
    assert outbox.sent[-1][1].startswith("Tu código")


async def test_a_code_works_once(client: AsyncClient, outbox: Outbox) -> None:
    await _ask(client)
    code = outbox.last_code()
    assert (await _verify(client, code)).status_code == 200
    response = await _verify(client, code)
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_code"


async def test_a_new_code_replaces_the_old_one(client: AsyncClient, outbox: Outbox) -> None:
    await _ask(client)
    old = outbox.last_code()
    await _ask(client)
    if outbox.last_code() != old:
        assert (await _verify(client, old)).status_code == 401


async def test_wrong_guesses_are_limited(client: AsyncClient, outbox: Outbox) -> None:
    await _ask(client)
    code = outbox.last_code()
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(get_settings().login_code_max_attempts):
        assert (await _verify(client, wrong)).status_code == 401
    response = await _verify(client, code)
    assert response.status_code == 429


async def test_an_expired_code_is_refused(
    client: AsyncClient, outbox: Outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "login_code_ttl_minutes", -1)
    await _ask(client)
    assert (await _verify(client, outbox.last_code())).status_code == 401


async def test_mail_down_is_reported(client: AsyncClient) -> None:
    app.dependency_overrides[get_mailer] = _Down
    response = await client.post("/api/v1/auth/code", json={"email": "ada@example.org"})
    assert response.status_code == 503
    assert response.json()["detail"] == "mail_unavailable"


async def test_me_needs_a_valid_token(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    response = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


async def test_logout_ends_the_session(client: AsyncClient, outbox: Outbox) -> None:
    await _ask(client)
    token = (await _verify(client, outbox.last_code())).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def _signed_in(client: AsyncClient, outbox: Outbox) -> dict[str, str]:
    await _ask(client)
    token = (await _verify(client, outbox.last_code())).json()["token"]
    return {"Authorization": f"Bearer {token}"}


async def _age(db: AsyncSession, **moved: timedelta) -> None:
    """Moves every session's timestamps back by the given amounts."""
    db.expire_all()  # read what the API wrote since
    for session in await db.scalars(select(Session)):
        for field, delta in moved.items():
            setattr(session, field, getattr(session, field) - delta)
    await db.commit()


async def test_a_session_ends_when_left_idle(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    headers = await _signed_in(client, outbox)
    idle = timedelta(minutes=get_settings().session_idle_minutes)
    await _age(db, last_seen_at=idle - timedelta(minutes=1))
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    # That request counted as use: the idle time starts again.
    await _age(db, last_seen_at=idle - timedelta(minutes=1))
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    await _age(db, last_seen_at=idle + timedelta(minutes=1))
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_a_session_ends_after_its_lifetime_however_used(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    headers = await _signed_in(client, outbox)
    await _age(db, expires_at=timedelta(hours=get_settings().session_ttl_hours))
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_signing_in_sweeps_ended_sessions(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    await _signed_in(client, outbox)
    await db.execute(update(Session).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    await db.commit()
    await _signed_in(client, outbox)
    db.expire_all()
    assert len(list(await db.scalars(select(Session)))) == 1


async def test_the_alias_is_set_and_cleared(client: AsyncClient, outbox: Outbox) -> None:
    await _ask(client)
    token = (await _verify(client, outbox.last_code())).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()["alias"] is None

    response = await client.patch("/api/v1/auth/me", json={"alias": "  Ada  "}, headers=headers)
    assert response.status_code == 200
    assert response.json()["alias"] == "Ada"
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()["alias"] == "Ada"

    cleared = await client.patch("/api/v1/auth/me", json={"alias": "   "}, headers=headers)
    assert cleared.json()["alias"] is None
    too_long = await client.patch("/api/v1/auth/me", json={"alias": "x" * 101}, headers=headers)
    assert too_long.status_code == 422
    assert (await client.patch("/api/v1/auth/me", json={"alias": "Eve"})).status_code == 401
