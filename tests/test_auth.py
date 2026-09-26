import re
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from registry_api.config import get_settings
from registry_api.db import get_session
from registry_api.mail import Locale, Mailer, get_mailer
from registry_api.main import app
from registry_api.models import Base


class _Outbox(Mailer):
    """Keeps what would have been sent."""

    def __init__(self) -> None:
        super().__init__(get_settings())
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))

    def last_code(self) -> str:
        match = re.search(r"\b(\d{6})\b", self.sent[-1][2])
        assert match
        return match.group(1)


class _Down(Mailer):
    def __init__(self) -> None:
        super().__init__(get_settings())

    async def send_code(self, to: str, code: str, locale: Locale) -> None:
        raise ConnectionRefusedError


@pytest.fixture
def outbox() -> _Outbox:
    box = _Outbox()
    app.dependency_overrides[get_mailer] = lambda: box
    return box


@pytest.fixture(autouse=True)
async def database() -> AsyncIterator[None]:
    """A fresh in-memory database per test (create_all is for tests only)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    yield
    await engine.dispose()


async def _ask(client: AsyncClient, email: str = "Ada@Example.org", locale: str = "en") -> None:
    response = await client.post("/api/v1/auth/code", json={"email": email, "locale": locale})
    assert response.status_code == 202, response.text


async def _verify(client: AsyncClient, code: str, email: str = "ada@example.org") -> Response:
    return await client.post("/api/v1/auth/verify", json={"email": email, "code": code})


async def test_code_signs_up_then_in(client: AsyncClient, outbox: _Outbox) -> None:
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


async def test_the_email_speaks_the_portal_language(client: AsyncClient, outbox: _Outbox) -> None:
    await _ask(client, locale="es")
    assert outbox.sent[-1][1].startswith("Tu código")


async def test_a_code_works_once(client: AsyncClient, outbox: _Outbox) -> None:
    await _ask(client)
    code = outbox.last_code()
    assert (await _verify(client, code)).status_code == 200
    response = await _verify(client, code)
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_code"


async def test_a_new_code_replaces_the_old_one(client: AsyncClient, outbox: _Outbox) -> None:
    await _ask(client)
    old = outbox.last_code()
    await _ask(client)
    if outbox.last_code() != old:
        assert (await _verify(client, old)).status_code == 401


async def test_wrong_guesses_are_limited(client: AsyncClient, outbox: _Outbox) -> None:
    await _ask(client)
    code = outbox.last_code()
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(get_settings().login_code_max_attempts):
        assert (await _verify(client, wrong)).status_code == 401
    response = await _verify(client, code)
    assert response.status_code == 429


async def test_an_expired_code_is_refused(
    client: AsyncClient, outbox: _Outbox, monkeypatch: pytest.MonkeyPatch
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


async def test_logout_ends_the_session(client: AsyncClient, outbox: _Outbox) -> None:
    await _ask(client)
    token = (await _verify(client, outbox.last_code())).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
