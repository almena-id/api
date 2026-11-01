"""The page behind the wallet's links on the identity domain (``/w/…``).

``https://almena.id/w/<what follows almena://>`` is the wallet's link (see
`registry_api.wallet`): on a phone with the wallet, iOS and Android hand it to
the app before any browser sees it (``/.well-known/apple-app-site-association``
and ``assetlinks.json``). Whoever opens it anywhere else — no wallet, a
computer, a link opened from this same domain — gets this page: it says what
the link is for, offers the same link as ``almena://`` (same path and query)
and where to get the wallet. No script; nothing it is sent appears unescaped.
"""

import base64
import hashlib
from datetime import UTC, datetime
from html import escape
from importlib.resources import files
from string import Template
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from registry_api import wallet
from registry_api.api.routes.home import FAVICON, mark
from registry_api.config import Settings, get_settings

router = APIRouter(include_in_schema=False)

_ASSETS = files("registry_api") / "assets"
_TEMPLATE = Template((_ASSETS / "wallet-link.html").read_text(encoding="utf-8"))
# The API's page, with a button; its typefaces come from the API's own host.
_CSS = (_ASSETS / "home.css").read_text(encoding="utf-8") + (
    ".button{display:inline-flex;align-items:center;justify-content:center;min-height:44px;"
    "padding:.65rem 1.5rem;border-radius:999px;background:var(--brand);color:#fff;"
    "font-weight:600;text-decoration:none}"
    ".button:hover{filter:brightness(1.1)}"
    ".lead a{color:var(--text)}"
)
# Longer than any link the platform makes (an invitation is a few KiB).
MOST = 16 * 1024


def scheme_link(path: str, query: str) -> str:
    """The same link as the wallet's own scheme: ``almena://{path}?{query}``."""
    link = wallet.SCHEME + quote(path, safe="/;:@&=+$,-_.!~*'()%")
    return f"{link}?{query}" if query else link


def page(*, link: str, settings: Settings, year: int) -> tuple[str, str]:
    """The page, whole, and the hash of its one style sheet for the CSP."""
    css = _CSS.replace("url(/fonts/", f"url({settings.public_url.rstrip('/')}/fonts/")
    html = _TEMPLATE.substitute(
        favicon=FAVICON,
        css=css,
        logo=mark(28),
        small=mark(18),
        link=escape(link, quote=True),
        wallet_url=escape(settings.wallet_url, quote=True),
        year=year,
    )
    digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
    return html, digest


@router.get("/w/{path:path}", response_class=HTMLResponse)
async def wallet_link(
    path: str, request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> HTMLResponse:
    query = request.url.query
    if len(path) + len(query) > MOST:
        raise HTTPException(status.HTTP_414_URI_TOO_LONG)
    html, digest = page(
        link=scheme_link(path, query), settings=settings, year=datetime.now(UTC).year
    )
    fonts = settings.public_url.rstrip("/")
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; style-src 'sha256-{digest}'; font-src {fonts}; "
                "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            # The link carries a request's address: it goes nowhere else.
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-store",
        },
    )
