"""The ways into an account, managed by the signed-in person.

An account has an email (optional) and any number of provider accounts
(Google, Microsoft, Apple, GitHub). Linking one proves it the usual way — the
emailed code, the provider's own sign-in — and unlinking never takes away the
last way in.

A way in that already belongs to another account is not taken from it: the
answer says `taken`. When the account doing the linking is still empty (it
works in no tenant but an untouched one of its own), the answer also carries a
ticket for ``POST /auth/me/move``, which hands its ways in to the owner, deletes
it and signs in as the owner — the proof just given shows the caller holds that
account too.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from registry_api import oauth
from registry_api.api.routes.auth import (
    CurrentSession,
    DbSession,
    OAuthCallback,
    OAuthStart,
    SignedIn,
    accept_invitations,
    as_utc,
    begin_oauth,
    check_code,
    finish_oauth,
    get_http_client,
    known_provider,
    sign_in,
)
from registry_api.models import (
    AccountMove,
    Identity,
    Issuer,
    Mediator,
    Session,
    Tenant,
    TenantInvitation,
    TenantMember,
    User,
    UserIdentity,
    Verifier,
)
from registry_api.security import digest, new_token

router = APIRouter(prefix="/auth/me", tags=["account"])


# `almena`: a wallet; its subject, a `did:key`, is shown instead of an email.
AccountProvider = oauth.ProviderId | Literal["almena"]


class LinkedAccount(BaseModel):
    id: uuid.UUID
    provider: AccountProvider
    # A wallet's DID; `null` for the other providers.
    did: str | None
    # What the provider showed when it was linked, if anything.
    email: str | None
    created_at: datetime


class WaysIn(BaseModel):
    email: str | None
    accounts: list[LinkedAccount]


class EmailLink(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")


class LinkResult(BaseModel):
    # `taken`: it belongs to another account, and stays there.
    status: Literal["linked", "taken"]
    # With `taken`, when this account is empty: for `POST /auth/me/move`.
    move_ticket: str | None = None


class MoveIn(BaseModel):
    ticket: str = Field(min_length=1, max_length=256)


async def _ways_in(db: AsyncSession, user: User) -> WaysIn:
    accounts = await db.scalars(
        select(UserIdentity)
        .where(UserIdentity.user_id == user.id)
        .order_by(UserIdentity.created_at, UserIdentity.id)
    )
    return WaysIn(
        email=user.email,
        accounts=[
            LinkedAccount(
                id=a.id,
                provider=known_provider_id(a.provider),
                did=a.subject if a.provider == "almena" else None,
                email=a.email,
                created_at=a.created_at,
            )
            for a in accounts
        ],
    )


def known_provider_id(provider: str) -> AccountProvider:
    if provider == "almena":
        return "almena"
    return next(p for p in oauth.PROVIDERS if p == provider)


async def _count_ways_in(db: AsyncSession, user: User) -> int:
    linked = await db.scalar(
        select(func.count()).select_from(UserIdentity).where(UserIdentity.user_id == user.id)
    )
    return (1 if user.email else 0) + (linked or 0)


async def own_empty_tenant(db: AsyncSession, user_id: uuid.UUID) -> Tenant | Literal[False] | None:
    """`False` unless the account is empty; else the one tenant it has, if any.

    Empty: it belongs to no tenant, or only to one it alone belongs to that is
    not the root and holds nothing but its own identity — no issuers,
    verifiers, mediators or invitations.
    """
    tenant_ids = list(
        await db.scalars(select(TenantMember.tenant_id).where(TenantMember.user_id == user_id))
    )
    if not tenant_ids:
        return None
    if len(tenant_ids) > 1:
        return False
    tenant = await db.get_one(Tenant, tenant_ids[0])
    if tenant.root:
        return False

    async def count(column: InstrumentedAttribute[uuid.UUID]) -> int:
        query = select(func.count()).where(column == tenant.id)
        return (await db.scalar(query)) or 0

    if await count(TenantMember.tenant_id) > 1 or await count(Identity.tenant_id) > 1:
        return False
    for column in (
        Issuer.tenant_id,
        Verifier.tenant_id,
        Mediator.tenant_id,
        TenantInvitation.tenant_id,
    ):
        if await count(column):
            return False
    return tenant


async def settle_taken(db: AsyncSession, user: User, owner_id: uuid.UUID) -> LinkResult:
    """The answer to linking a way in that `owner_id` holds (maybe this account itself)."""
    if owner_id == user.id:
        return LinkResult(status="linked")
    if await own_empty_tenant(db, user.id) is False:
        await db.commit()
        return LinkResult(status="taken")
    ticket = new_token()
    db.add(
        AccountMove(
            ticket_hash=digest(ticket),
            from_user_id=user.id,
            to_user_id=owner_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db.commit()
    return LinkResult(status="taken", move_ticket=ticket)


@router.get("/ways-in", summary="The signed-in account's email and linked provider accounts")
async def ways_in(session: CurrentSession, db: DbSession) -> WaysIn:
    return await _ways_in(db, session.user)


@router.post(
    "/email",
    summary="Link an email, proved by the code sent to it (replaces the one there was)",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "`invalid_code`: wrong or expired"},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "`too_many_attempts`: ask again"},
    },
)
async def link_email(body: EmailLink, session: CurrentSession, db: DbSession) -> LinkResult:
    email = body.email.lower()
    await check_code(db, email, body.code)
    user = session.user
    owner = await db.scalar(select(User.id).where(User.email == email))
    if owner is not None:
        return await settle_taken(db, user, owner)
    user.email = email
    await db.flush()
    await accept_invitations(db, user)
    await db.commit()
    return LinkResult(status="linked")


@router.delete(
    "/email",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink the email",
    responses={status.HTTP_409_CONFLICT: {"description": "`last_way_in`"}},
)
async def unlink_email(session: CurrentSession, db: DbSession) -> None:
    user = session.user
    if user.email is None:
        return
    if await _count_ways_in(db, user) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="last_way_in")
    user.email = None
    await db.commit()


@router.post(
    "/accounts/{provider}/start",
    summary="Begin linking a provider account: where to send the browser",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`unknown_provider`"},
        status.HTTP_409_CONFLICT: {"description": "`provider_disabled`: not configured"},
    },
)
async def link_start(provider: str, session: CurrentSession, db: DbSession) -> OAuthStart:
    return await begin_oauth(db, known_provider(provider), session.user_id)


@router.post(
    "/accounts/{provider}/callback",
    summary="Finish linking a provider account",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "`invalid_state`: unknown, used, expired or another account's"
        },
        status.HTTP_502_BAD_GATEWAY: {"description": "`provider_error`"},
    },
)
async def link_callback(
    provider: str,
    body: OAuthCallback,
    session: CurrentSession,
    db: DbSession,
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> LinkResult:
    provider_id = known_provider(provider)
    identity = await finish_oauth(db, http, provider_id, body, session.user_id)
    linked = await db.scalar(
        select(UserIdentity).where(
            UserIdentity.provider == provider_id, UserIdentity.subject == identity.subject
        )
    )
    if linked is not None:
        return await settle_taken(db, session.user, linked.user_id)
    db.add(
        UserIdentity(
            user_id=session.user_id,
            provider=provider_id,
            subject=identity.subject,
            email=identity.email if identity.email_verified else None,
        )
    )
    await db.commit()
    return LinkResult(status="linked")


@router.delete(
    "/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink a provider account",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`account_not_found`"},
        status.HTTP_409_CONFLICT: {"description": "`last_way_in`"},
    },
)
async def unlink_account(account_id: uuid.UUID, session: CurrentSession, db: DbSession) -> None:
    account = await db.get(UserIdentity, account_id)
    if account is None or account.user_id != session.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account_not_found")
    if await _count_ways_in(db, session.user) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="last_way_in")
    await db.delete(account)
    await db.commit()


@router.post(
    "/move",
    summary="Leave this empty account for the one a way in belongs to, and sign in there",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "`invalid_ticket`: unknown, used or expired"},
        status.HTTP_409_CONFLICT: {"description": "`not_empty`: it holds something now"},
    },
)
async def move(body: MoveIn, session: CurrentSession, db: DbSession) -> SignedIn:
    ticket = await db.scalar(
        select(AccountMove).where(AccountMove.ticket_hash == digest(body.ticket))
    )
    if (
        ticket is None
        or ticket.from_user_id != session.user_id
        or as_utc(ticket.expires_at) <= datetime.now(UTC)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_ticket")
    owner = await db.get_one(User, ticket.to_user_id)
    await db.delete(ticket)
    tenant = await own_empty_tenant(db, session.user_id)
    if tenant is False:
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="not_empty")

    user = session.user
    # Its ways in go with it: the owner gains them. An email only while the
    # owner has none — an account has one.
    await db.execute(
        update(UserIdentity).where(UserIdentity.user_id == user.id).values(user_id=owner.id)
    )
    moved_email = user.email if owner.email is None else None
    if tenant is not None:
        tenant.identity_id = None
        await db.flush()
        await db.execute(delete(Identity).where(Identity.tenant_id == tenant.id))
        await db.execute(delete(TenantMember).where(TenantMember.tenant_id == tenant.id))
        await db.delete(tenant)
    await db.execute(delete(Session).where(Session.user_id == user.id))
    await db.execute(delete(AccountMove).where(AccountMove.from_user_id == user.id))
    await db.delete(user)
    await db.flush()
    if moved_email is not None:
        owner.email = moved_email
    return await sign_in(db, owner)
