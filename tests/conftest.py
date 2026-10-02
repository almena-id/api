import re
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from registry_api.config import Settings, get_settings
from registry_api.db import get_session
from registry_api.dns_proof import get_txt_lookup
from registry_api.mail import Mailer, get_mailer
from registry_api.main import app
from registry_api.models import Base
from registry_api.vault import get_vault
from registry_api.vault.memory import MemoryVault


class Outbox(Mailer):
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


@pytest.fixture
def outbox() -> Outbox:
    box = Outbox()
    app.dependency_overrides[get_mailer] = lambda: box
    return box


@pytest.fixture(autouse=True)
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
    yield sessionmaker
    await engine.dispose()


@pytest.fixture(autouse=True)
def vault() -> MemoryVault:
    """A vault in memory per test, in place of OpenBao."""
    store = MemoryVault()
    app.dependency_overrides[get_vault] = lambda: store
    return store


@pytest.fixture
async def db(database: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """A session on the test's database, for what no endpoint does."""
    async with database() as session:
        yield session


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Google, Microsoft and GitHub configured, the portal at http://portal.test."""
    s = get_settings()
    monkeypatch.setattr(s, "portal_url", "http://portal.test")
    for name, value in {
        "google_client_id": "google-id",
        "google_client_secret": "google-secret",
        "microsoft_client_id": "ms-id",
        "microsoft_client_secret": "ms-secret",
        "github_client_id": "gh-id",
        "github_client_secret": "gh-secret",
    }.items():
        monkeypatch.setattr(s, name, value if name.endswith("_id") else SecretStr(value))
    yield s


class Dns:
    """TXT records by name, as the lookup finds them."""

    def __init__(self) -> None:
        self.records: dict[str, list[str]] = {}

    async def lookup(self, name: str) -> list[str]:
        return self.records.get(name, [])


@pytest.fixture
def dns() -> Dns:
    fake = Dns()
    app.dependency_overrides[get_txt_lookup] = lambda: fake.lookup
    return fake
