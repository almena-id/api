"""Refusals: always `{"detail": "<code>"}`, a stable code the portals word,
and what more says why beside it — `field` or `credential` (an index in what
was sent), `errors` (problems by key). One shape for every error, so a client
reads the code in one place."""

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class Refusal(HTTPException):
    """A refusal with its code and, beside it, what more says why."""

    def __init__(
        self,
        status_code: int,
        code: str,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(status_code, detail=code, headers=headers)
        self.extra = extra


async def handle(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, Refusal)
    return JSONResponse(
        {"detail": error.detail, **error.extra},
        status_code=error.status_code,
        headers=error.headers,
    )
