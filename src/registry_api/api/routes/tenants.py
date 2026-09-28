"""Tenants the signed-in user belongs to, and a tenant's own details."""

import uuid
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from registry_api import mediators
from registry_api.api.routes.auth import CurrentSession, DbSession, get_http_client
from registry_api.api.routes.directory import IdentityRef, TenantId
from registry_api.api.routes.members import admin_of
from registry_api.models import Identity, Tenant, TenantMember

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str | None
    created_at: datetime
    # The signed-in user's role in it: what the portal offers depends on it.
    role: str


class TenantDetail(TenantOut):
    # The mailbox of every issuer and verifier in the tenant; `null` until chosen.
    mediator_url: str | None
    mediator_did: str | None
    # The organisation's own identity.
    identity: IdentityRef | None


class TenantIn(BaseModel):
    # Each field is changed only when sent; `mediator_url: null` removes it.
    name: str | None = Field(default=None, max_length=200)
    mediator_url: str | None = Field(default=None, max_length=2048)


async def _detail(db: DbSession, tenant: Tenant, role: str) -> TenantDetail:
    identity = await db.get(Identity, tenant.identity_id) if tenant.identity_id else None
    return TenantDetail(
        id=tenant.id,
        name=tenant.name,
        created_at=tenant.created_at,
        role=role,
        mediator_url=tenant.mediator_url,
        mediator_did=tenant.mediator_did,
        identity=IdentityRef(id=identity.id, name=identity.name) if identity else None,
    )


@router.get("", summary="The signed-in user's tenants, oldest first")
async def list_tenants(session: CurrentSession, db: DbSession) -> list[TenantOut]:
    rows = await db.execute(
        select(Tenant, TenantMember.role)
        .join(TenantMember, TenantMember.tenant_id == Tenant.id)
        .where(TenantMember.user_id == session.user_id)
        .order_by(Tenant.created_at, Tenant.id)
    )
    return [TenantOut(id=t.id, name=t.name, created_at=t.created_at, role=role) for t, role in rows]


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
            "description": "`name_required`, `mediator_invalid`, `mediator_insecure` "
            "(HTTP off loopback), `mediator_unreachable`, `mediator_not_a_mediator`"
        },
    },
)
async def update_tenant(
    tenant_id: Annotated[uuid.UUID, Depends(admin_of)],
    body: TenantIn,
    db: DbSession,
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
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
    if "mediator_url" in sent:
        if not (body.mediator_url or "").strip():
            tenant.mediator_url = tenant.mediator_did = None
        else:
            try:
                url, did = await mediators.resolve(http, body.mediator_url or "")
            except mediators.MediatorError as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, detail=error.code
                ) from None
            tenant.mediator_url, tenant.mediator_did = url, did
    await db.commit()
    await db.refresh(tenant)
    return await _detail(db, tenant, "admin")
