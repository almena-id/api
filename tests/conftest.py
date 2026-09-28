import re
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from registry_api.config import get_settings
from registry_api.db import get_session
from registry_api.mail import Mailer, get_mailer
from registry_api.main import app
from registry_api.models import Base


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


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
