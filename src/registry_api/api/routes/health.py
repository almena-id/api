"""Liveness and readiness probes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import __version__
from registry_api.db import get_session
from registry_api.vault import Vault, get_vault

router = APIRouter(tags=["health"])

State = Literal["ok", "unavailable"]


class Health(BaseModel):
    status: State
    version: str
    database: State | None = None
    vault: State | None = None


@router.get("/health", summary="Liveness: the process is up")
async def health() -> Health:
    return Health(status="ok", version=__version__)


@router.get(
    "/health/ready",
    summary="Readiness: the database and the vault answer",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Health}},
)
async def ready(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    vault: Annotated[Vault, Depends(get_vault)],
) -> Health:
    try:
        await session.execute(text("SELECT 1"))
        database: State = "ok"
    except (SQLAlchemyError, OSError):
        database = "unavailable"
    vault_state: State = "ok" if await vault.ping() else "unavailable"
    ok = database == "ok" and vault_state == "ok"
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Health(
        status="ok" if ok else "unavailable",
        version=__version__,
        database=database,
        vault=vault_state,
    )
