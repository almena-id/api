from collections.abc import AsyncIterator

from httpx import AsyncClient
from sqlalchemy.exc import OperationalError

from registry_api import __version__
from registry_api.db import get_session
from registry_api.main import app


class _FailingSession:
    async def execute(self, *_: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


class _OkSession:
    async def execute(self, *_: object) -> None:
        return None


async def _failing() -> AsyncIterator[_FailingSession]:
    yield _FailingSession()


async def _ok() -> AsyncIterator[_OkSession]:
    yield _OkSession()


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "database": None}


async def test_ready_when_database_answers(client: AsyncClient) -> None:
    app.dependency_overrides[get_session] = _ok
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


async def test_ready_when_database_is_down(client: AsyncClient) -> None:
    app.dependency_overrides[get_session] = _failing
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


async def test_docs_are_served_by_scalar(client: AsyncClient) -> None:
    response = await client.get("/docs")
    assert response.status_code == 200
    assert "@scalar/api-reference" in response.text
    assert "/openapi.json" in response.text
