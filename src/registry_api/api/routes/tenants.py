"""Tenants the signed-in user belongs to, a tenant's own details and its health."""

import uuid
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from registry_api import entitlements, tenant_health
from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.api.routes.directory import IdentityRef, MediatorRef, TenantId, tenant_mediator
from registry_api.api.routes.members import admin_of
from registry_api.api.routes.signing import Signer, signer_out
from registry_api.field_catalog import LANGUAGES
from registry_api.models import Identity, Mediator, Tenant, TenantMember
from registry_api.signing_flows import Flow, signs

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str | None
    created_at: datetime
    # The signed-in user's role in it: what the portal offers depends on it.
    role: str
    # Whether they sign as the tenant under its flow (a wallet aside): the
    # portal offers them signing and publishing.
    signs: bool
    # The trust anchor (the root): its catalogue is everyone's
    # (`registry_api.trust_anchor`).
    anchor: bool
    # What its subscription lets it do (`registry_api.entitlements`).
    features: list[str]
    # The languages it works in, of the platform's, in the platform's order.
    languages: list[str]


class TenantDetail(TenantOut):
    # The organisation's own identity, and the mediator it receives messages
    # through (one of the tenant's or a public one; `null` until chosen).
    identity: IdentityRef | None
    mediator: MediatorRef | None
    # Who signs as the tenant (`signing_flows`); `single_user`: who.
    signing_flow: Flow
    signer: Signer | None


class TenantIn(BaseModel):
    # Each field is changed only when sent; `mediator_id: null` removes it.
    name: str | None = Field(default=None, max_length=200)
    mediator_id: uuid.UUID | None = None
    signing_flow: Flow | None = None
    # `single_user`: the member who signs.
    signer_id: uuid.UUID | None = None
    # Of the platform's languages, one at least; the trust anchor's are all.
    languages: list[str] | None = Field(default=None, max_length=len(LANGUAGES))


def _languages(given: list[str], anchor: bool) -> list[str]:
    """The languages as kept, in the platform's order, or why they cannot be:
    none, one the platform does not speak, one twice — or, for the trust
    anchor, any short of all (`languages_anchor`)."""
    if not given or len(set(given)) != len(given) or set(given) - set(LANGUAGES):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="languages_invalid")
    if anchor and set(given) != set(LANGUAGES):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="languages_anchor")
    return [lang for lang in LANGUAGES if lang in given]


async def _detail(db: DbSession, tenant: Tenant, role: str, user_id: uuid.UUID) -> TenantDetail:
    identity = await db.get(Identity, tenant.identity_id) if tenant.identity_id else None
    mediator = await db.get(Mediator, tenant.mediator_id) if tenant.mediator_id else None
    return TenantDetail(
        id=tenant.id,
        name=tenant.name,
        created_at=tenant.created_at,
        role=role,
        signs=await signs(db, tenant, user_id),
        anchor=tenant.root,
        features=await entitlements.features(db, tenant),
        languages=tenant.languages,
        identity=IdentityRef(id=identity.id, name=identity.name, did=identity.did)
        if identity
        else None,
        mediator=MediatorRef(
            id=mediator.id, name=mediator.name, own=mediator.tenant_id == tenant.id
        )
        if mediator
        else None,
        signing_flow=cast(Flow, tenant.signing_flow),
        signer=await signer_out(db, tenant.id, tenant.signer_id),
    )


@router.get("", summary="The signed-in user's tenants, oldest first")
async def list_tenants(session: CurrentSession, db: DbSession) -> list[TenantOut]:
    rows = await db.execute(
        select(Tenant, TenantMember.role)
        .join(TenantMember, TenantMember.tenant_id == Tenant.id)
        .where(TenantMember.user_id == session.user_id)
        .order_by(Tenant.created_at, Tenant.id)
    )
    return [
        TenantOut(
            id=t.id,
            name=t.name,
            created_at=t.created_at,
            role=role,
            signs=await signs(db, t, session.user_id),
            anchor=t.root,
            features=await entitlements.features(db, t),
            languages=t.languages,
        )
        for t, role in rows
    ]


@router.get("/{tenant_id}", summary="A tenant's details")
async def get_tenant(tenant_id: TenantId, session: CurrentSession, db: DbSession) -> TenantDetail:
    member = await db.get_one(TenantMember, (tenant_id, session.user_id))
    return await _detail(db, await db.get_one(Tenant, tenant_id), member.role, session.user_id)


@router.patch(
    "/{tenant_id}",
    summary="Change a tenant's name, mediator, signing flow or languages (admins only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_admin`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`name_required`, `mediator_not_found` (neither the tenant's nor a "
            "public one), "
            "`signer_required` (`single_user` with nobody), `signer_not_member`, "
            "`languages_invalid` (none, repeated or not the platform's), "
            "`languages_anchor` (the trust anchor works in all of them)"
        },
    },
)
async def update_tenant(
    tenant_id: Annotated[uuid.UUID, Depends(admin_of)],
    body: TenantIn,
    session: CurrentSession,
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
    if "languages" in sent:
        tenant.languages = _languages(body.languages or [], tenant.root)
    if "mediator_id" in sent:
        tenant.mediator_id = await tenant_mediator(db, tenant_id, body.mediator_id)
    if "signer_id" in sent and body.signer_id is not None:
        if await db.get(TenantMember, (tenant_id, body.signer_id)) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="signer_not_member")
        tenant.signer_id = body.signer_id
    # There is always a flow: `null` leaves it as it is.
    if body.signing_flow is not None:
        tenant.signing_flow = body.signing_flow
    # Only `single_user` names somebody, and it cannot name nobody.
    if tenant.signing_flow != "single_user":
        tenant.signer_id = None
    elif tenant.signer_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="signer_required")
    await db.commit()
    await db.refresh(tenant)
    return await _detail(db, tenant, "admin", session.user_id)


class HealthCheck(BaseModel):
    check: tenant_health.Check
    done: bool
    # What is missing, when not done: `missing` (name, mediator), `unpublished`
    # (mediator), `no_signer` or `no_wallet` (signing flow).
    issue: tenant_health.Issue | None


class TenantHealth(BaseModel):
    # The share of checks done, 0 to 100.
    score: int
    checks: list[HealthCheck]


@router.get("/{tenant_id}/health", summary="What the tenant still needs set up to operate")
async def get_health(tenant_id: TenantId, db: DbSession) -> TenantHealth:
    results = await tenant_health.checks(db, await db.get_one(Tenant, tenant_id))
    return TenantHealth(
        score=tenant_health.score(results),
        checks=[HealthCheck(check=r.check, done=r.issue is None, issue=r.issue) for r in results],
    )
