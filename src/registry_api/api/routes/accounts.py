"""Subscriptions: each tenant reads its own; the trust anchor manages them all.

A tenant's members see its subscription and the features it gives
(`GET /tenants/{id}/subscription`). Until payments arrive, the trust anchor's
admins grant and end them by hand: they list every other tenant — an
*account* — with its subscription, and set or remove it
(`/tenants/{anchor_id}/accounts…`; `anchor_only` from any other tenant,
`not_admin` from a member who is not one).
"""

import base64
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import entitlements
from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.api.routes.members import admin_of
from registry_api.models import Subscription, SubscriptionStatus, Tenant, TenantMember

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["subscriptions"])


class SubscriptionOut(BaseModel):
    # `null` with no subscription: the free use of the platform.
    plan: str | None
    status: SubscriptionStatus | None
    current_period_end: datetime | None
    # Whether it gives its plan's features now (`entitlements.in_force`).
    in_force: bool
    # What the tenant may do with it (the trust anchor: everything).
    features: list[str]
    updated_at: datetime | None


async def _out(db: AsyncSession, tenant: Tenant) -> SubscriptionOut:
    item = await db.get(Subscription, tenant.id)
    return SubscriptionOut(
        plan=item.plan if item else None,
        status=cast(SubscriptionStatus, item.status) if item else None,
        current_period_end=item.current_period_end if item else None,
        in_force=entitlements.in_force(item),
        features=list(await entitlements.features(db, tenant)),
        updated_at=item.updated_at if item else None,
    )


@router.get("/subscription", summary="The tenant's subscription and the features it gives")
async def get_subscription(tenant_id: TenantId, db: DbSession) -> SubscriptionOut:
    return await _out(db, await db.get_one(Tenant, tenant_id))


async def anchor_admin(
    tenant_id: Annotated[uuid.UUID, Depends(admin_of)], db: DbSession
) -> uuid.UUID:
    """The trust anchor, asked by one of its admins."""
    tenant = await db.get_one(Tenant, tenant_id)
    if not tenant.root:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="anchor_only")
    return tenant_id


AnchorAdmin = Annotated[uuid.UUID, Depends(anchor_admin)]


class AccountOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str | None
    created_at: datetime
    members: int
    subscription: SubscriptionOut
    # The anchor's own note on it (an invoice, an agreement); never shown to
    # the account.
    note: str | None


class AccountPage(BaseModel):
    items: list[AccountOut]
    # Pass it back as `cursor` for the next page; `null` on the last one.
    next_cursor: str | None


def _encode(tenant: Tenant) -> str:
    raw = f"{tenant.created_at.isoformat()}|{tenant.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        moment, tenant_id = raw.split("|")
        return datetime.fromisoformat(moment), uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_cursor") from None


async def _account(db: AsyncSession, tenant: Tenant) -> AccountOut:
    members = await db.scalar(
        select(func.count()).select_from(TenantMember).where(TenantMember.tenant_id == tenant.id)
    )
    item = await db.get(Subscription, tenant.id)
    return AccountOut(
        note=item.note if item else None,
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        created_at=tenant.created_at,
        members=members or 0,
        subscription=await _out(db, tenant),
    )


@router.get(
    "/accounts",
    summary="The trust anchor: every other tenant with its subscription, newest first",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "`invalid_cursor`"},
        status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`, `not_admin`"},
    },
)
async def list_accounts(
    tenant_id: AnchorAdmin,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    subscribed: Annotated[
        Literal["yes", "no"] | None,
        Query(description="`yes`: those with a subscription in any state; `no`: without"),
    ] = None,
) -> AccountPage:
    query = select(Tenant).where(Tenant.id != tenant_id)
    text = (q or "").strip()
    if text:
        # A part of its name, whatever the case, or its slug.
        query = query.where(
            or_(
                func.lower(Tenant.name).contains(text.lower(), autoescape=True),
                Tenant.slug == text,
            )
        )
    if subscribed:
        held = select(Subscription.tenant_id)
        query = query.where(Tenant.id.in_(held) if subscribed == "yes" else Tenant.id.not_in(held))
    if cursor:
        moment, last = _decode(cursor)
        query = query.where(
            or_(Tenant.created_at < moment, and_(Tenant.created_at == moment, Tenant.id < last))
        )
    rows = list(
        await db.scalars(
            query.order_by(Tenant.created_at.desc(), Tenant.id.desc()).limit(limit + 1)
        )
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return AccountPage(
        items=[await _account(db, row) for row in rows],
        next_cursor=_encode(rows[-1]) if more else None,
    )


async def _other(db: AsyncSession, anchor: uuid.UUID, account_id: uuid.UUID) -> Tenant:
    tenant = await db.get(Tenant, account_id)
    if tenant is None or tenant.id == anchor:
        # The anchor has everything already, and no subscription.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account_not_found")
    return tenant


@router.get(
    "/accounts/{account_id}",
    summary="The trust anchor: one account with its subscription",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`, `not_admin`"},
        status.HTTP_404_NOT_FOUND: {"description": "`account_not_found`"},
    },
)
async def get_account(tenant_id: AnchorAdmin, account_id: uuid.UUID, db: DbSession) -> AccountOut:
    return await _account(db, await _other(db, tenant_id, account_id))


class SubscriptionIn(BaseModel):
    plan: str = Field(max_length=32)
    status: SubscriptionStatus
    # Until when it is paid; none, open-ended. Never in the past while active.
    current_period_end: datetime | None = None
    note: str | None = Field(default=None, max_length=1000)


@router.put(
    "/accounts/{account_id}/subscription",
    summary="The trust anchor: set an account's subscription",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`, `not_admin`"},
        status.HTTP_404_NOT_FOUND: {"description": "`account_not_found`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`plan_invalid`: not one of the plans; `period_end_invalid`: "
            "an active one's end is already past"
        },
    },
)
async def set_subscription(
    tenant_id: AnchorAdmin,
    account_id: uuid.UUID,
    body: SubscriptionIn,
    session: CurrentSession,
    db: DbSession,
) -> AccountOut:
    account = await _other(db, tenant_id, account_id)
    if body.plan not in entitlements.PLANS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="plan_invalid")
    end = entitlements.as_utc(body.current_period_end) if body.current_period_end else None
    if body.status == "active" and end is not None and end <= datetime.now(UTC):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="period_end_invalid")
    item = await db.get(Subscription, account.id)
    if item is None:
        item = Subscription(tenant_id=account.id)
        db.add(item)
    item.plan = body.plan
    item.status = body.status
    item.current_period_end = end
    item.note = (body.note or "").strip() or None
    item.updated_by = session.user_id
    await db.commit()
    await db.refresh(item)
    return await _account(db, account)


@router.delete(
    "/accounts/{account_id}/subscription",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="The trust anchor: remove an account's subscription (back to the free use)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`, `not_admin`"},
        status.HTTP_404_NOT_FOUND: {"description": "`account_not_found`"},
    },
)
async def remove_subscription(
    tenant_id: AnchorAdmin, account_id: uuid.UUID, db: DbSession
) -> Response:
    account = await _other(db, tenant_id, account_id)
    item = await db.get(Subscription, account.id)
    if item is not None:
        await db.delete(item)
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
