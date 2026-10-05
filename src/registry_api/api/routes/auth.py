"""Portal accounts, without passwords: one flow for signing up and signing in.

1. ``POST /auth/code`` emails a six-digit code to the address.
2. ``POST /auth/verify`` exchanges it for a session, creating the account (and
   an unnamed tenant it belongs to) the first time that address signs in.

Or through a provider (Google, Microsoft, Apple, GitHub): ``/auth/oauth/…/start``
gives the portal the URL to send the browser to, and ``…/callback`` turns what
the provider sent back into a session. A provider account signing in for the
first time joins the account with the address it vouches for, or starts one.

An account may have other ways in besides its email, or none at all but those:
``account.py`` links and unlinks them.

A session is an opaque bearer token; the portal keeps it in an HTTP-only
cookie on its own origin and sends it as ``Authorization: Bearer``. It ends
``session_ttl_hours`` after signing in, or after ``session_idle_minutes`` unused.
The CLI keeps it in the system's keychain; for scripts and CI an account makes
API tokens (``tokens.py``), sent the same way.
Errors carry a stable code in ``detail`` for the portal to translate.
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import logs, oauth
from registry_api.config import get_settings
from registry_api.db import get_session
from registry_api.mail import Locale, Mailer, get_mailer
from registry_api.models import (
    Identity,
    LoginCode,
    OAuthFlow,
    Session,
    Tenant,
    TenantInvitation,
    TenantMember,
    User,
    UserIdentity,
)
from registry_api.root import default_mediator
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
    # The portal's language: names the tenant a new account starts with.
    locale: Locale = "en"


class UserOut(BaseModel):
    id: uuid.UUID
    # `null` for an account that signs in only through a provider or its wallet.
    email: str | None
    alias: str | None
    created_at: datetime


class AccountIn(BaseModel):
    # Blank clears it.
    alias: str | None = Field(default=None, max_length=100)


class SignedIn(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


class Provider(BaseModel):
    id: oauth.ProviderId
    enabled: bool


class Providers(BaseModel):
    providers: list[Provider]


class OAuthStart(BaseModel):
    authorization_url: str
    # Kept by the portal in a cookie, so the answer is bound to this browser.
    state: str


class OAuthCallback(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=256)
    # The portal's language: names the tenant a new account starts with.
    locale: Locale = "en"


async def get_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """FastAPI dependency; tests override it with a mock transport."""
    async with httpx.AsyncClient(timeout=10) as client:
        yield client


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, alias=user.alias, created_at=user.created_at)


def _code_hash(email: str, code: str) -> bytes:
    # Bound to the address, so a code is worth nothing for any other one.
    return digest(f"{email}:{code}")


def as_utc(moment: datetime) -> datetime:
    # PostgreSQL hands back aware datetimes; SQLite (the tests) naive UTC ones.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


_SEEN_EVERY = timedelta(minutes=1)


def _idle() -> timedelta:
    return timedelta(minutes=get_settings().session_idle_minutes)


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
    now = datetime.now(UTC)
    session = await db.scalar(
        select(Session).where(
            Session.token_hash == digest(credentials.credentials),
            Session.expires_at > now,
            # An API token (named) is never idle.
            Session.name.is_not(None) | (Session.last_seen_at > now - _idle()),
        )
    )
    if session is None:
        raise unauthorized
    logs.set_user(session.user_id)
    # Written at most once a minute: idle time is not worth a write per request.
    if as_utc(session.last_seen_at) <= now - _SEEN_EVERY:
        session.last_seen_at = now
        await db.commit()
    return session


CurrentSession = Annotated[Session, Depends(current_session)]


async def optional_session(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Session | None:
    """The session when a bearer token comes and holds; `None` without one."""
    if credentials is None:
        return None
    return await current_session(db, credentials)


OptionalSession = Annotated[Session | None, Depends(optional_session)]


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
    await check_code(db, email, body.code)
    return await sign_in(db, await _user_for_email(db, email, body.locale))


async def check_code(db: AsyncSession, email: str, code: str) -> None:
    """Spend the code emailed to `email`, or refuse (`invalid_code`, `too_many_attempts`)."""
    settings = get_settings()
    login_code = await db.scalar(select(LoginCode).where(LoginCode.email == email))
    if login_code is None or as_utc(login_code.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_code")
    if login_code.attempts >= settings.login_code_max_attempts:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts")
    if not hmac.compare_digest(login_code.code_hash, _code_hash(email, code)):
        login_code.attempts += 1
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_code")
    # A code is good once.
    await db.delete(login_code)


async def new_tenant(
    db: AsyncSession,
    name: str | None,
    identity_name: str | None = None,
    languages: list[str] | None = None,
) -> Tenant:
    """A tenant, with its own identity named like it (or `identity_name`,
    unnamed), receiving messages through the default mediator (the root's),
    working in `languages` (its creator's, English when unsaid)."""
    mediator = await default_mediator(db)
    tenant = Tenant(
        name=name, mediator_id=mediator.id if mediator else None, languages=languages or ["en"]
    )
    db.add(tenant)
    await db.flush()
    identity = Identity(
        tenant_id=tenant.id, name=name or identity_name or "", created_at=datetime.now(UTC)
    )
    db.add(identity)
    await db.flush()
    tenant.identity_id = identity.id
    await db.flush()
    return tenant


_TENANT_NAME: dict[Locale, str] = {"en": "Tenant of {email}", "es": "Tenant de {email}"}


def default_tenant_name(email: str, locale: Locale) -> str:
    """What the tenant an account starts with is called, until it is renamed."""
    # tenants.name holds 200 characters; an address may be longer.
    return _TENANT_NAME[locale].format(email=email)[:200]


async def _user_for_email(db: AsyncSession, email: str, locale: Locale = "en") -> User:
    """The account for an address the caller has verified, created if new."""
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email)
        db.add(user)
        invited = await db.scalar(
            select(TenantInvitation.id).where(TenantInvitation.email == email).limit(1)
        )
        if invited is None:
            # A new account starts with a tenant of its own, named after it and
            # run by it, so there is always one to work in — unless somebody
            # already asked it into theirs: then that is the one it works in.
            tenant = await new_tenant(db, default_tenant_name(email, locale), languages=[locale])
            db.add(TenantMember(tenant_id=tenant.id, user_id=user.id, role="admin"))
        await db.flush()
        await db.refresh(user)
    return user


async def accept_invitations(db: AsyncSession, user: User) -> None:
    """Signing in with an address, or linking it, accepts the invitations sent to it."""
    if user.email is None:
        return
    invitations = list(
        await db.scalars(select(TenantInvitation).where(TenantInvitation.email == user.email))
    )
    for invitation in invitations:
        if await db.get(TenantMember, (invitation.tenant_id, user.id)) is None:
            db.add(
                TenantMember(tenant_id=invitation.tenant_id, user_id=user.id, role=invitation.role)
            )
        await db.delete(invitation)
    await db.flush()


async def sign_in(db: AsyncSession, user: User) -> SignedIn:
    await accept_invitations(db, user)
    now = datetime.now(UTC)
    # Ended sessions are swept here, not by a job: signing in is frequent enough.
    await db.execute(
        delete(Session).where(
            (Session.expires_at <= now)
            | (Session.name.is_(None) & (Session.last_seen_at <= now - _idle()))
        )
    )
    token = new_token()
    expires_at = now + timedelta(hours=get_settings().session_ttl_hours)
    db.add(
        Session(user_id=user.id, token_hash=digest(token), expires_at=expires_at, last_seen_at=now)
    )
    await db.commit()
    return SignedIn(token=token, expires_at=expires_at, user=_user_out(user))


def known_provider(provider: str) -> oauth.ProviderId:
    known = next((p for p in oauth.PROVIDERS if p == provider), None)
    if known is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown_provider")
    if not oauth.is_enabled(get_settings(), known):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="provider_disabled")
    return known


@router.get("/providers", summary="Social sign-in providers, and which are configured")
async def providers() -> Providers:
    settings = get_settings()
    return Providers(
        providers=[Provider(id=p, enabled=oauth.is_enabled(settings, p)) for p in oauth.PROVIDERS]
    )


@router.post(
    "/oauth/{provider}/start",
    summary="Begin a social sign-in: where to send the browser",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`unknown_provider`"},
        status.HTTP_409_CONFLICT: {"description": "`provider_disabled`: not configured"},
    },
)
async def oauth_start(provider: str, db: DbSession) -> OAuthStart:
    return await begin_oauth(db, known_provider(provider))


async def begin_oauth(
    db: AsyncSession, provider_id: oauth.ProviderId, user_id: uuid.UUID | None = None
) -> OAuthStart:
    """A flow to a provider; `user_id` when that account is linking it, not signing in."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    nonce = secrets.token_urlsafe(24)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    db.add(
        OAuthFlow(
            state_hash=digest(state),
            provider=provider_id,
            code_verifier=verifier,
            nonce=nonce,
            user_id=user_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db.commit()
    url = oauth.authorization_url(
        get_settings(), provider_id, state=state, challenge=challenge, nonce=nonce
    )
    return OAuthStart(authorization_url=url, state=state)


@router.post(
    "/oauth/{provider}/callback",
    summary="Finish a social sign-in (creates the account the first time)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "`invalid_state`: unknown, used or expired"},
        status.HTTP_403_FORBIDDEN: {
            "description": "`email_unverified`: the provider does not vouch for an email"
        },
        status.HTTP_502_BAD_GATEWAY: {"description": "`provider_error`"},
    },
)
async def oauth_callback(
    provider: str,
    body: OAuthCallback,
    db: DbSession,
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> SignedIn:
    provider_id = known_provider(provider)
    identity = await finish_oauth(db, http, provider_id, body)

    linked = await db.scalar(
        select(UserIdentity).where(
            UserIdentity.provider == provider_id, UserIdentity.subject == identity.subject
        )
    )
    if linked is not None:
        return await sign_in(db, await db.get_one(User, linked.user_id))

    # Only an address the provider vouches for may create an account or join
    # one: otherwise anyone could sign in as somebody else's email.
    if not (identity.email and identity.email_verified):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="email_unverified")
    user = await _user_for_email(db, identity.email, body.locale)
    db.add(
        UserIdentity(
            user_id=user.id,
            provider=provider_id,
            subject=identity.subject,
            email=identity.email,
        )
    )
    return await sign_in(db, user)


async def finish_oauth(
    db: AsyncSession,
    http: httpx.AsyncClient,
    provider_id: oauth.ProviderId,
    body: OAuthCallback,
    user_id: uuid.UUID | None = None,
) -> oauth.Identity:
    """What the provider says about the account, for the flow `body.state` names.

    A flow started to link a provider (`user_id`) finishes only for that
    account, and a sign-in flow only as a sign-in.
    """
    flow = await db.scalar(
        select(OAuthFlow).where(
            OAuthFlow.state_hash == digest(body.state), OAuthFlow.provider == provider_id
        )
    )
    if flow is None or as_utc(flow.expires_at) <= datetime.now(UTC) or flow.user_id != user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_state")
    # A flow is good once, whatever the provider says next.
    verifier, nonce = flow.code_verifier, flow.nonce
    await db.delete(flow)
    await db.commit()

    try:
        return await oauth.exchange(
            get_settings(), provider_id, http, code=body.code, verifier=verifier, nonce=nonce
        )
    except oauth.OAuthError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=error.code) from None


@router.get("/me", summary="The signed-in account")
async def me(session: CurrentSession) -> UserOut:
    return _user_out(session.user)


@router.patch("/me", summary="Change the signed-in account's own details (its alias)")
async def update_me(body: AccountIn, session: CurrentSession, db: DbSession) -> UserOut:
    user = session.user
    user.alias = (body.alias or "").strip() or None
    await db.commit()
    await db.refresh(user)
    return _user_out(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End this session")
async def logout(session: CurrentSession, db: DbSession) -> None:
    await db.execute(delete(Session).where(Session.id == session.id))
    await db.commit()
