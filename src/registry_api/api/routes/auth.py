"""Portal accounts, without passwords: one flow for signing up and signing in.

1. ``POST /auth/code`` emails a six-digit code to the address.
2. ``POST /auth/verify`` exchanges it for a session, creating the account the
   first time that address signs in.

A session is an opaque bearer token; the portal keeps it in an HTTP-only
cookie on its own origin and sends it as ``Authorization: Bearer``.
Errors carry a stable code in ``detail`` for the portal to translate.
"""

import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.config import get_settings
from registry_api.db import get_session
from registry_api.mail import Mailer, get_mailer
from registry_api.models import LoginCode, Session, User
from registry_api.security import digest, new_code, new_token

router = APIRouter(prefix="/auth", tags=["auth"])

_bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_session)]


class CodeRequest(BaseModel):
    email: EmailStr
    # The language the email is written in: the portal's current one.
    locale: Literal["en", "es"] = "en"


class CodeCheck(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    created_at: datetime


class SignedIn(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, created_at=user.created_at)


def _code_hash(email: str, code: str) -> bytes:
    # Bound to the address, so a code is worth nothing for any other one.
    return digest(f"{email}:{code}")


def _as_utc(moment: datetime) -> datetime:
    # PostgreSQL hands back aware datetimes; SQLite (the tests) naive UTC ones.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def current_session(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Session:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="not_authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    session = await db.scalar(
        select(Session).where(
            Session.token_hash == digest(credentials.credentials),
            Session.expires_at > datetime.now(UTC),
        )
    )
    if session is None:
        raise unauthorized
    return session


CurrentSession = Annotated[Session, Depends(current_session)]


@router.post(
    "/code",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a sign-in code (replaces any earlier one)",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "`mail_unavailable`"}},
)
async def send_code(
    body: CodeRequest, db: DbSession, mailer: Annotated[Mailer, Depends(get_mailer)]
) -> None:
    email = body.email.lower()
    code = new_code()
    settings = get_settings()
    await db.execute(delete(LoginCode).where(LoginCode.email == email))
    db.add(
        LoginCode(
            email=email,
            code_hash=_code_hash(email, code),
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.login_code_ttl_minutes),
        )
    )
    await db.commit()
    try:
        await mailer.send_code(email, code, body.locale)
    except OSError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="mail_unavailable"
        ) from None


@router.post(
    "/verify",
    summary="Exchange an emailed code for a session (creates the account the first time)",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "`invalid_code`: wrong or expired"},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "`too_many_attempts`: ask again"},
    },
)
async def verify_code(body: CodeCheck, db: DbSession) -> SignedIn:
    email = body.email.lower()
    settings = get_settings()
    login_code = await db.scalar(select(LoginCode).where(LoginCode.email == email))
    if login_code is None or _as_utc(login_code.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_code")
    if login_code.attempts >= settings.login_code_max_attempts:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts")
    if not hmac.compare_digest(login_code.code_hash, _code_hash(email, body.code)):
        login_code.attempts += 1
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_code")

    # A code is good once.
    await db.delete(login_code)
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email)
        db.add(user)
        await db.flush()
        await db.refresh(user)

    token = new_token()
    expires_at = datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)
    db.add(Session(user_id=user.id, token_hash=digest(token), expires_at=expires_at))
    await db.commit()
    return SignedIn(token=token, expires_at=expires_at, user=_user_out(user))


@router.get("/me", summary="The signed-in account")
async def me(session: CurrentSession) -> UserOut:
    return _user_out(session.user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End this session")
async def logout(session: CurrentSession, db: DbSession) -> None:
    await db.execute(delete(Session).where(Session.id == session.id))
    await db.commit()
