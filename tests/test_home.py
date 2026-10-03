from collections.abc import AsyncIterator

from httpx import AsyncClient
from sqlalchemy.exc import OperationalError

from registry_api import __version__
from registry_api.api.routes.home import FONTS, page
from registry_api.config import get_settings
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


async def test_home_names_the_api_and_links_its_reference(client: AsyncClient) -> None:
    html = (await client.get("/")).text
    assert "<h1>Almena <span>API</span></h1>" in html
    assert "Registry API" not in html
    settings = get_settings()
    assert f'<dt>Docu URL</dt><dd><a href="{settings.public_url}/docs">' in html


def test_home_omits_the_docu_url_when_the_reference_is_off() -> None:
    settings = get_settings().model_copy(update={"environment": "production"})
    html = page(ready=True, version=__version__, settings=settings, year=2026)
    assert "Docu URL" not in html
    assert "Base URL" in html
