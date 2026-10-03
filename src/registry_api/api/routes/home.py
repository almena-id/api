"""The page a browser shows at the API's root (api.almena.id).

What `/health/ready` already makes public — status, version, the database —
under the header and footer of Almena's portals, in the API's identity colour,
red. Nothing about tenants or traffic.

The typefaces are the portals' (SIL Open Font License, in `assets/fonts`),
served by the API itself from `/fonts/{name}`.
"""

from datetime import UTC, datetime
from html import escape
from importlib.resources import files
from string import Template
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import __version__
from registry_api.config import Settings, get_settings
from registry_api.db import get_session

router = APIRouter(include_in_schema=False)

FONTS = (
    "chakra-petch-500.woff2",
    "chakra-petch-600.woff2",
    "chakra-petch-700.woff2",
    "inter.woff2",
    "jetbrains-mono.woff2",
)

# The Almena mark (three nodes and their links), in the current colour.
_MARK = (
    '<svg width="{size}" height="{size}" viewBox="136 136 752 752" aria-hidden="true">'
    '<g stroke="currentColor" stroke-width="24" stroke-linecap="round">'
    '<line x1="512" y1="237" x2="237" y2="785"/><line x1="512" y1="237" x2="785" y2="785"/>'
    '<line x1="237" y1="785" x2="646" y2="507"/></g><g fill="currentColor">'
    '<circle cx="512" cy="237" r="94"/><circle cx="237" cy="785" r="94"/>'
    '<circle cx="785" cy="785" r="94"/></g></svg>'
)

BRAND = "#e0413a"


def _mark(size: int) -> str:
    return _MARK.format(size=size)


# The favicon is the mark itself, in red, so the API needs no icon file.
_FAVICON = "data:image/svg+xml," + quote(
    _MARK.format(size=64)
    .replace('aria-hidden="true"', 'xmlns="http://www.w3.org/2000/svg"')
    .replace("currentColor", BRAND)
)

# The page and its styles live in `assets/`: home.html, a `string.Template`,
# and home.css, the wallet's dark tokens with its red accent (green and amber
# carry the status only) under the portals' header and footer.
_ASSETS = files("registry_api") / "assets"
_TEMPLATE = Template((_ASSETS / "home.html").read_text(encoding="utf-8"))
_CSS = (_ASSETS / "home.css").read_text(encoding="utf-8")


def _docs_row(settings: Settings) -> str:
    """The Docu URL row, only when the reference is served."""
    if not settings.docs_enabled:
        return ""
    url = escape(f"{settings.public_url}/docs")
    return f'<dt>Docu URL</dt><dd><a href="{url}"><code>{url}</code></a></dd>\n'


def page(*, ready: bool, version: str, settings: Settings, year: int) -> str:
    """The root page, whole."""
    state, label = ("ok", "Operational") if ready else ("down", "Degraded")
    return _TEMPLATE.substitute(
        favicon=_FAVICON,
        css=_CSS,
        logo=_mark(28),
        small=_mark(18),
        state=state,
        label=label,
        version=escape(version),
        database="Connected" if ready else "Unavailable",
        base_url=escape(f"{settings.public_url}/api/v1"),
        docs=_docs_row(settings),
        year=year,
    )


@router.get("/", response_class=HTMLResponse)
async def home(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    try:
        await session.execute(text("SELECT 1"))
        ready = True
    except (SQLAlchemyError, OSError):
        ready = False
    return HTMLResponse(
        page(ready=ready, version=__version__, settings=settings, year=datetime.now(UTC).year)
    )


@router.get("/fonts/{name}")
async def font(name: str) -> Response:
    if name not in FONTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    data = (_ASSETS / "fonts" / name).read_bytes()
    return Response(
        data,
        media_type="font/woff2",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
