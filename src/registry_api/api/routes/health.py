"""Liveness and readiness probes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import __version__
from registry_api.db import get_session

router = APIRouter(tags=["health"])

State = Literal["ok", "unavailable"]


class Health(BaseModel):
    status: State
    version: str
    database: State | None = None


@router.get("/health", summary="Liveness: the process is up")
async def health() -> Health:
    return Health(status="ok", version=__version__)


@router.get(
    "/health/ready",
    summary="Readiness: the database answers",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Health}},
)
async def ready(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Health:
    try:
        await session.execute(text("SELECT 1"))
        database: State = "ok"
    except (SQLAlchemyError, OSError):
        database = "unavailable"
    ok = database == "ok"
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Health(
        status="ok" if ok else "unavailable",
        version=__version__,
        database=database,
    )
