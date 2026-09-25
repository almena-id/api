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


class Health(BaseModel):
    status: Literal["ok", "unavailable"]
    version: str
    database: Literal["ok", "unavailable"] | None = None


@router.get("/health", summary="Liveness: the process is up")
async def health() -> Health:
    return Health(status="ok", version=__version__)


@router.get(
    "/health/ready",
    summary="Readiness: the database answers",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Health}},
)
async def ready(
    response: Response, session: Annotated[AsyncSession, Depends(get_session)]
) -> Health:
    try:
        await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(status="unavailable", version=__version__, database="unavailable")
    return Health(status="ok", version=__version__, database="ok")
