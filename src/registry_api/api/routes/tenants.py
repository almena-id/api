"""Tenants the signed-in user belongs to, and a tenant's own details."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Exists, exists, select

from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.api.routes.directory import IdentityRef, MediatorRef, TenantId, tenant_mediator
from registry_api.api.routes.members import admin_of
from registry_api.models import Certification, Identity, Mediator, Tenant, TenantMember

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str | None
    created_at: datetime
    # The signed-in user's role in it: what the portal offers depends on it.
    role: str
    # Almena has approved a certification of it, in force now.
    certified: bool


class TenantDetail(TenantOut):
    # The organisation's own identity, and the mediator it receives messages
    # through (one of the tenant's; `null` until chosen).
    identity: IdentityRef | None
    mediator: MediatorRef | None


class TenantIn(BaseModel):
    # Each field is changed only when sent; `mediator_id: null` removes it.
    name: str | None = Field(default=None, max_length=200)
    mediator_id: uuid.UUID | None = None


def _certified(tenant_id: Any) -> Exists:
    return exists().where(Certification.tenant_id == tenant_id, Certification.status == "approved")


async def _detail(db: DbSession, tenant: Tenant, role: str) -> TenantDetail:
    identity = await db.get(Identity, tenant.identity_id) if tenant.identity_id else None
    mediator = await db.get(Mediator, tenant.mediator_id) if tenant.mediator_id else None
    return TenantDetail(
        id=tenant.id,
        name=tenant.name,
        created_at=tenant.created_at,
        role=role,
        certified=bool(await db.scalar(select(_certified(tenant.id)))),
        identity=IdentityRef(id=identity.id, name=identity.name) if identity else None,
        mediator=MediatorRef(id=mediator.id, name=mediator.name) if mediator else None,
    )


@router.get("", summary="The signed-in user's tenants, oldest first")
async def list_tenants(session: CurrentSession, db: DbSession) -> list[TenantOut]:
    rows = await db.execute(
        select(Tenant, TenantMember.role, _certified(Tenant.id))
        .join(TenantMember, TenantMember.tenant_id == Tenant.id)
        .where(TenantMember.user_id == session.user_id)
        .order_by(Tenant.created_at, Tenant.id)
    )
    return [
        TenantOut(id=t.id, name=t.name, created_at=t.created_at, role=role, certified=certified)
        for t, role, certified in rows
    ]


@router.get("/{tenant_id}", summary="A tenant's details")
async def get_tenant(tenant_id: TenantId, session: CurrentSession, db: DbSession) -> TenantDetail:
    member = await db.get_one(TenantMember, (tenant_id, session.user_id))
    return await _detail(db, await db.get_one(Tenant, tenant_id), member.role)


@router.patch(
    "/{tenant_id}",
    summary="Change a tenant's name or mediator (admins only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_admin`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`name_required`, `mediator_not_found` (not one of the tenant's)"
        },
    },
)
async def update_tenant(
    tenant_id: Annotated[uuid.UUID, Depends(admin_of)],
    body: TenantIn,
    db: DbSession,
) -> TenantDetail:
    tenant = await db.get_one(Tenant, tenant_id)
    sent = body.model_fields_set
    if "name" in sent:
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="name_required")
        # The tenant's own identity is named like it, and stays so.
        if tenant.identity_id and tenant.name is not None:
            identity = await db.get(Identity, tenant.identity_id)
            if identity is not None and identity.name == tenant.name:
                identity.name = name
        tenant.name = name
    if "mediator_id" in sent:
        tenant.mediator_id = await tenant_mediator(db, tenant_id, body.mediator_id)
    await db.commit()
    await db.refresh(tenant)
    return await _detail(db, tenant, "admin")
