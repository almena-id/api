"""API tokens: long-lived bearer tokens an account makes for scripts and CI.

A token is a session with a name (``models.session``): sent as
``Authorization: Bearer``, it acts as the account that made it, everywhere a
sign-in does. It lasts the days it was made for (``expires_in_days``, up to a
year) and never ends for being idle. Its secret is shown once, when it is made;
only its SHA-256 is kept.

Only a sign-in makes tokens: a token cannot make another, so a leaked one
cannot outlive the date it was given.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.models import Session
from registry_api.security import digest, new_token

router = APIRouter(prefix="/auth/me/tokens", tags=["account"])

# What a token's secret starts with: tells it apart (and lets secret scanners
# spot one) from a sign-in's.
PREFIX = "almena_"
MAX_DAYS = 365
DEFAULT_DAYS = 90
# Tokens an account may hold at once.
MAX_TOKENS = 50


class TokenIn(BaseModel):
    name: str = Field(max_length=100)
    expires_in_days: int = Field(default=DEFAULT_DAYS, ge=1, le=MAX_DAYS)


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    expires_at: datetime
    # Updated at most once a minute.
    last_used_at: datetime


class TokenMade(TokenOut):
    # Shown this once.
    token: str


class TokenList(BaseModel):
    items: list[TokenOut]


def _out(session: Session) -> TokenOut:
    assert session.name is not None
    return TokenOut(
        id=session.id,
        name=session.name,
        created_at=session.created_at,
        expires_at=session.expires_at,
        last_used_at=session.last_seen_at,
    )


@router.get("", summary="The account's API tokens, newest first (never their secrets)")
async def list_tokens(session: CurrentSession, db: DbSession) -> TokenList:
    tokens = await db.scalars(
        select(Session)
        .where(
            Session.user_id == session.user_id,
            Session.name.is_not(None),
            Session.expires_at > datetime.now(UTC),
        )
        .order_by(Session.created_at.desc(), Session.id)
    )
    return TokenList(items=[_out(token) for token in tokens])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Make an API token (from a sign-in only); its secret is shown this once",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`sign_in_required`: a token cannot make one"},
        status.HTTP_409_CONFLICT: {"description": "`too_many_tokens`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "`name_required`"},
    },
)
async def create_token(body: TokenIn, session: CurrentSession, db: DbSession) -> TokenMade:
    if session.name is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="sign_in_required")
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="name_required")
    now = datetime.now(UTC)
    # Expired ones go as new ones come.
    await db.execute(
        delete(Session).where(
            Session.user_id == session.user_id,
            Session.name.is_not(None),
            Session.expires_at <= now,
        )
    )
    held = await db.scalar(
        select(func.count())
        .select_from(Session)
        .where(Session.user_id == session.user_id, Session.name.is_not(None))
    )
    if (held or 0) >= MAX_TOKENS:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="too_many_tokens")
    secret = PREFIX + new_token()
    token = Session(
        user_id=session.user_id,
        name=name,
        token_hash=digest(secret),
        expires_at=now + timedelta(days=body.expires_in_days),
        last_seen_at=now,
        created_at=now,
    )
    db.add(token)
    await db.commit()
    return TokenMade(**_out(token).model_dump(), token=secret)


@router.delete(
    "/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API token",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`token_not_found`"}},
)
async def delete_token(token_id: uuid.UUID, session: CurrentSession, db: DbSession) -> None:
    token = await db.get(Session, token_id)
    if token is None or token.user_id != session.user_id or token.name is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="token_not_found")
    await db.delete(token)
    await db.commit()
