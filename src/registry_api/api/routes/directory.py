"""What a tenant holds: its issuers, verifiers and identities.

Identities are the tenant's register of DIDs. Every issuer and verifier acts
as one of them — a new one named like it, or one chosen from the register, so
an organisation can issue and verify with the same DID.

Everything lives under ``/tenants/{tenant_id}/…`` and is only reachable by the
tenant's members. Lists are newest first and paged by an opaque cursor, so the
portal can keep scrolling however long they grow; each page also says how many
there are in all.
"""

import base64
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select

from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.models import Identity, Issuer, Tenant, TenantMember, Verifier

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["directory"])

Item = Issuer | Verifier | Identity


async def member_tenant(tenant_id: uuid.UUID, session: CurrentSession, db: DbSession) -> uuid.UUID:
    """The tenant in the path, if the signed-in user belongs to it."""
    member = await db.get(TenantMember, (tenant_id, session.user_id))
    if member is None:
        # Not "forbidden": whether somebody else's tenant exists is not ours to say.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="tenant_not_found")
    return tenant_id


TenantId = Annotated[uuid.UUID, Depends(member_tenant)]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=200)]


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


class NamedIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name is empty")
        return value


class DescribedIn(NamedIn):
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("description")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return _clean(value)


class IdentityRef(BaseModel):
    id: uuid.UUID
    name: str


class Use(BaseModel):
    kind: Literal["tenant", "issuer", "verifier"]
    id: uuid.UUID
    name: str


class RoleIn(DescribedIn):
    # The identity to act as; left out, a new one is created with the same name.
    identity_id: uuid.UUID | None = None


class ItemOut(BaseModel):
    id: uuid.UUID
    name: str
    # Issuers and verifiers carry one; identities do not.
    description: str | None = None
    created_at: datetime
    # Issuers and verifiers: the identity they act as.
    identity: IdentityRef | None = None
    # Identities: the tenant, issuers and verifiers that act as them.
    used_by: list[Use] | None = None


class Page(BaseModel):
    items: list[ItemOut]
    # Pass it back as `cursor` for the next page; `null` on the last one.
    next_cursor: str | None
    total: int


def _encode(item: Item) -> str:
    raw = f"{item.created_at.isoformat()}|{item.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        moment, item_id = raw.split("|")
        return datetime.fromisoformat(moment), uuid.UUID(item_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_cursor") from None


def _out(item: Item, used_by: list[Use] | None = None) -> ItemOut:
    identity = None
    if isinstance(item, Issuer | Verifier):
        identity = IdentityRef(id=item.identity.id, name=item.identity.name)
    return ItemOut(
        id=item.id,
        name=item.name,
        description=getattr(item, "description", None),
        created_at=item.created_at,
        identity=identity,
        used_by=used_by,
    )


async def _uses(db: DbSession, identity_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Use]]:
    """For each identity, the tenant, issuers and verifiers that act as it."""
    uses: dict[uuid.UUID, list[Use]] = {i: [] for i in identity_ids}
    tenants = await db.execute(
        select(Tenant.identity_id, Tenant.id, Tenant.name).where(
            Tenant.identity_id.in_(identity_ids)
        )
    )
    for identity_id, tenant_id, name in tenants:
        if identity_id is not None:
            uses[identity_id].append(Use(kind="tenant", id=tenant_id, name=name or ""))
    for kind, model in (("issuer", Issuer), ("verifier", Verifier)):
        rows = await db.execute(
            select(model.identity_id, model.id, model.name)
            .where(model.identity_id.in_(identity_ids))
            .order_by(model.name)
        )
        for identity_id, item_id, name in rows:
            uses[identity_id].append(Use(kind=kind, id=item_id, name=name))
    return uses


async def _page(
    db: DbSession, model: type[Item], tenant_id: uuid.UUID, limit: int, cursor: str | None
) -> Page:
    owned: Any = model.tenant_id == tenant_id
    query = select(model).where(owned)
    if cursor:
        moment, item_id = _decode(cursor)
        # Keyset paging: strictly after the last one seen, in (created_at, id) order.
        query = query.where(
            or_(
                model.created_at < moment,
                and_(model.created_at == moment, model.id < item_id),
            )
        )
    rows = cast(
        list[Item],
        list(
            await db.scalars(
                query.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
            )
        ),
    )
    total = await db.scalar(select(func.count()).select_from(model).where(owned)) or 0
    more = len(rows) > limit
    rows = rows[:limit]
    uses = await _uses(db, [r.id for r in rows]) if model is Identity else {}
    return Page(
        items=[_out(row, uses.get(row.id) if model is Identity else None) for row in rows],
        next_cursor=_encode(rows[-1]) if more else None,
        total=total,
    )


async def _create(db: DbSession, item: Item) -> ItemOut:
    # Stamped here rather than by the database, to the microsecond, so the
    # (created_at, id) order the cursor walks is the order of creation.
    item.created_at = datetime.now(UTC)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    if isinstance(item, Issuer | Verifier):
        await db.refresh(item, ["identity"])
    return _out(item, [] if isinstance(item, Identity) else None)


async def _identity_for(db: DbSession, tenant_id: uuid.UUID, body: RoleIn) -> uuid.UUID:
    """The identity an issuer or verifier acts as: the one chosen, which must be
    the tenant's, or a new one named like it."""
    if body.identity_id is not None:
        identity = await db.get(Identity, body.identity_id)
        if identity is None or identity.tenant_id != tenant_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="identity_not_found")
        return identity.id
    identity = Identity(tenant_id=tenant_id, name=body.name, created_at=datetime.now(UTC))
    db.add(identity)
    await db.flush()
    return identity.id


@router.get("/issuers", summary="The tenant's issuers, newest first")
async def list_issuers(
    tenant_id: TenantId, db: DbSession, limit: Limit = 20, cursor: Cursor = None
) -> Page:
    return await _page(db, Issuer, tenant_id, limit, cursor)


@router.post("/issuers", status_code=status.HTTP_201_CREATED, summary="Register an issuer")
async def create_issuer(tenant_id: TenantId, body: RoleIn, db: DbSession) -> ItemOut:
    identity_id = await _identity_for(db, tenant_id, body)
    return await _create(
        db,
        Issuer(
            tenant_id=tenant_id,
            name=body.name,
            description=body.description,
            identity_id=identity_id,
        ),
    )


@router.get("/verifiers", summary="The tenant's verifiers, newest first")
async def list_verifiers(
    tenant_id: TenantId, db: DbSession, limit: Limit = 20, cursor: Cursor = None
) -> Page:
    return await _page(db, Verifier, tenant_id, limit, cursor)


@router.post("/verifiers", status_code=status.HTTP_201_CREATED, summary="Register a verifier")
async def create_verifier(tenant_id: TenantId, body: RoleIn, db: DbSession) -> ItemOut:
    identity_id = await _identity_for(db, tenant_id, body)
    return await _create(
        db,
        Verifier(
            tenant_id=tenant_id,
            name=body.name,
            description=body.description,
            identity_id=identity_id,
        ),
    )


@router.get("/identities", summary="The tenant's identities, newest first")
async def list_identities(
    tenant_id: TenantId, db: DbSession, limit: Limit = 20, cursor: Cursor = None
) -> Page:
    return await _page(db, Identity, tenant_id, limit, cursor)


@router.post("/identities", status_code=status.HTTP_201_CREATED, summary="Register an identity")
async def create_identity(tenant_id: TenantId, body: NamedIn, db: DbSession) -> ItemOut:
    return await _create(db, Identity(tenant_id=tenant_id, name=body.name))
