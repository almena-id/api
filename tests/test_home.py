from collections.abc import AsyncIterator

from httpx import AsyncClient
from sqlalchemy.exc import OperationalError

from registry_api import __version__
from registry_api.api.routes.home import FONTS
from registry_api.db import get_session
from registry_api.main import app


class _FailingSession:
    async def execute(self, *_: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


async def _failing() -> AsyncIterator[_FailingSession]:
    yield _FailingSession()


async def test_home_is_a_page_in_red(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "Operational" in html
    assert __version__ in html
    assert "--brand:#e0413a" in html
    assert "<nav>" not in html
    assert "<header>" in html and "<footer>" in html


async def test_home_says_degraded_when_database_is_down(client: AsyncClient) -> None:
    app.dependency_overrides[get_session] = _failing
    response = await client.get("/")
    assert response.status_code == 200
    assert "Degraded" in response.text


async def test_home_is_not_in_the_openapi_document(client: AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert "/" not in paths
    assert "/fonts/{name}" not in paths


async def test_every_font_the_page_asks_for_is_served(client: AsyncClient) -> None:
    html = (await client.get("/")).text
    for name in FONTS:
        assert f"/fonts/{name}" in html
        response = await client.get(f"/fonts/{name}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "font/woff2"
        assert response.content.startswith(b"wOF2")


async def test_unknown_fonts_are_not_found(client: AsyncClient) -> None:
    assert (await client.get("/fonts/OFL-inter.txt")).status_code == 404
    assert (await client.get("/fonts/..%2Fmain.py")).status_code == 404
