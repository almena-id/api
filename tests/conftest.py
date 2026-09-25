from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from registry_api.main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
