"""How much a request may send: bodies are read as they come, never whole
first, and refused (413, `body_too_large`, with the limit as `max_bytes`)
once past it.

`BodyLimit`, a middleware, holds every request to `MOST` (above the largest
file a holder uploads); `read` holds a route that reads its own body (the
wallets' `direct_post` answers) to its smaller limit.
"""

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from registry_api.errors import Refusal

# Any request, at most: a holder's file (10 MiB) and its multipart framing.
MOST = 11 * 1024 * 1024
# A wallet's answer with presentations (a `vp_token`).
PRESENTATION_MOST = 2 * 1024 * 1024
# A wallet's answer to sign in, link or sign (an `id_token`, a proof, a JWS).
ANSWER_MOST = 64 * 1024


def too_large(most: int) -> Refusal:
    return Refusal(status.HTTP_413_CONTENT_TOO_LARGE, "body_too_large", max_bytes=most)


def _declared(headers: list[tuple[bytes, bytes]]) -> int | None:
    for name, value in headers:
        if name == b"content-length" and value.isdigit():
            return int(value)
    return None


async def read(request: Request, most: int) -> bytes:
    """The request's body, `most` bytes at most, else `body_too_large`."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > most:
        raise too_large(most)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > most:
            raise too_large(most)
    return bytes(body)


class BodyLimit:
    """Refuses a request whose body is, or grows, past `most` bytes."""

    def __init__(self, app: ASGIApp, most: int = MOST) -> None:
        self.app = app
        self.most = most

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _declared(scope["headers"])
        if declared is not None and declared > self.most:
            response = JSONResponse(
                {"detail": "body_too_large", "max_bytes": self.most}, status_code=413
            )
            await response(scope, receive, send)
            return
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.most:
                    # Raised where the body is read, so answered as a refusal.
                    raise too_large(self.most)
            return message

        await self.app(scope, limited, send)
