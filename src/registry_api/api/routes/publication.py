"""What is done to an issuer, verifier or mediator: publish, unpublish and
delete (admins).

Publishing is what makes one visible outside its tenant.

Each starts as a draft, seen only by the tenant's members. Publishing is an
admin's decision, not a signature: once its own DID and the tenant's are
signed, it is published — its DID resolves, the catalogue lists it.
Unpublishing takes it back.

The catalogue names the tenant by its DID, never by the tenant's own name,
which starts as "Tenant of {email}".

Deleting one deletes its identity too (it is its own, never shared): its DID
stops resolving for good. A mediator's users are left without one.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update

from registry_api import paging
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import ItemDetail, item_detail
from registry_api.api.routes.members import admin_of
from registry_api.broker import Broker, BrokerError, get_broker
from registry_api.models import Identity, Issuer, Mediator, Tenant, Verifier
from registry_api.trust_anchor import PREFIX

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["publication"])
catalog = APIRouter(prefix="/catalog", tags=["catalog"])

AdminTenant = Annotated[uuid.UUID, Depends(admin_of)]
Kind = Literal["issuers", "verifiers", "mediators"]
Publishable = Issuer | Verifier | Mediator

MODELS: dict[str, type[Publishable]] = {
    "issuers": Issuer,
    "verifiers": Verifier,
    "mediators": Mediator,
}


async def _owned(
    db: DbSession, kind: Kind, tenant_id: uuid.UUID, item_id: uuid.UUID
) -> Publishable:
    item = cast(Publishable | None, await db.get(MODELS[kind], item_id))
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{kind[:-1]}_not_found")
    return item


async def _unpublish(db: DbSession, item: Publishable) -> ItemDetail:
    item.published_at = None
    await db.commit()
    await db.refresh(
        item, ["identity", "mediator"] if not isinstance(item, Mediator) else ["identity"]
    )
    return await item_detail(db, item)


@router.post(
    "/{kind}/{item_id}/publish",
    summary="Publish an issuer, verifier or mediator: its DID resolves, the catalogue lists it",
    responses={
        403: {"description": "`not_admin`"},
        409: {
            "description": "`identity_pending`: its identity is not signed yet; "
            "`tenant_pending`: the tenant's DID is not signed yet"
        },
    },
)
async def publish(
    tenant_id: AdminTenant, kind: Kind, item_id: uuid.UUID, db: DbSession
) -> ItemDetail:
    item = await _owned(db, kind, tenant_id, item_id)
    identity = await db.get_one(Identity, item.identity_id)
    tenant = await db.get_one(Tenant, tenant_id)
    owner = await db.get(Identity, tenant.identity_id) if tenant.identity_id else None
    # Nothing is published under a DID that does not exist yet.
    if identity.did is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="identity_pending")
    if owner is None or owner.did is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="tenant_pending")
    # Published again, it keeps the first moment.
    if item.published_at is None:
        item.published_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(
        item, ["identity", "mediator"] if not isinstance(item, Mediator) else ["identity"]
    )
    return await item_detail(db, item)


@router.post(
    "/{kind}/{item_id}/unpublish",
    summary="Take one back to a draft: its DID stops resolving, the catalogue drops it",
    responses={403: {"description": "`not_admin`"}},
)
async def unpublish(
    tenant_id: AdminTenant, kind: Kind, item_id: uuid.UUID, db: DbSession
) -> ItemDetail:
    return await _unpublish(db, await _owned(db, kind, tenant_id, item_id))


@router.delete(
    "/{kind}/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an issuer, verifier or mediator, and its identity",
    responses={403: {"description": "`not_admin`"}},
)
async def delete(
    tenant_id: AdminTenant,
    kind: Kind,
    item_id: uuid.UUID,
    db: DbSession,
    broker: Annotated[Broker, Depends(get_broker)],
) -> Response:
    item = await _owned(db, kind, tenant_id, item_id)
    # Its queue goes with it: what it named, before it is gone.
    queued = (
        (item.slug, item.queue_name)
        if isinstance(item, Issuer | Verifier) and item.queue_created_at
        else None
    )
    identity = await db.get(Identity, item.identity_id)
    if isinstance(item, Mediator):
        # The database would do it (SET NULL); said here so the session agrees.
        for model in (Tenant, Issuer, Verifier):
            await db.execute(
                update(model).where(model.mediator_id == item.id).values(mediator_id=None)
            )
    await db.delete(item)
    await db.flush()
    if identity is not None:
        await db.delete(identity)
    await db.commit()
    if queued is not None:
        try:
            await broker.remove(*queued)
        except BrokerError:
            logger.warning("queue_left", extra={"kind": kind, "item_id": str(item.id)})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class TenantPublic(BaseModel):
    did: str


class Entry(BaseModel):
    did: str
    name: str
    # Issuers and verifiers carry one; mediators do not.
    # By language.
    description: dict[str, str] | None = None
    # Mediators: where they listen.
    url: str | None = None
    # Issuers: their slug (where their offers are), the credential types they
    # grant (Almena's catalogue) and those they offer — with a form to apply.
    slug: str | None = None
    credential_types: list[str] | None = None
    offers: list[str] | None = None
    published_at: datetime
    tenant: TenantPublic


class CatalogPage(BaseModel):
    items: list[Entry]
    # Pass it back as `cursor` for the next page; `null` on the last one.
    next_cursor: str | None


async def _tenant(db: DbSession, tenant_id: uuid.UUID) -> TenantPublic:
    tenant = await db.get(Tenant, tenant_id)
    owner = await db.get(Identity, tenant.identity_id) if tenant and tenant.identity_id else None
    return TenantPublic(did=owner.did or "" if owner else "")


@catalog.get(
    "/{kind}",
    summary="Public: the published issuers, verifiers or mediators, newest first",
    responses={
        422: {
            "description": "`grants_issuers_only`: `grants` filters issuers; "
            "`tenant_required`: a tenant's own type (`custom:{key}`) is named in its tenant"
        }
    },
)
async def list_published(
    kind: Kind,
    db: DbSession,
    limit: paging.Limit = 20,
    cursor: paging.Cursor = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    grants: Annotated[str | None, Query(max_length=64)] = None,
    tenant: Annotated[str | None, Query(max_length=255)] = None,
) -> CatalogPage:
    model = MODELS[kind]
    if grants and kind != "issuers":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="grants_issuers_only")
    if grants and grants.startswith(PREFIX) and not tenant:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="tenant_required")
    # Only what resolves: published, under a DID that has been signed.
    query = (
        select(model)
        .join(Identity, Identity.id == model.identity_id)
        .where(model.published_at.is_not(None), Identity.did.is_not(None))
    )
    # Only a tenant's, by its DID (its own identity's).
    if tenant:
        owners = select(Tenant.id).join(Identity, Identity.id == Tenant.identity_id)
        query = query.where(model.tenant_id.in_(owners.where(Identity.did == tenant)))
    # Searching: a part of its name, whatever the case, or of its DID.
    text = (q or "").strip()
    if text:
        query = query.where(
            or_(
                func.lower(model.name).contains(text.lower(), autoescape=True),
                Identity.did.contains(text, autoescape=True),
            )
        )

    if cursor:
        query = query.where(paging.after(model.published_at, model.id, cursor))
    query = query.order_by(model.published_at.desc(), model.id.desc())
    # The types an issuer grants are a JSON list: those that grant `grants`
    # are picked here, a batch at a time, until the page is full.
    rows: list[Publishable] = []
    batch = query
    while True:
        found = cast(list[Publishable], list(await db.scalars(batch.limit(limit + 1))))
        rows += [
            row
            for row in found
            if not grants or grants in (cast(Issuer, row).credential_types or [])
        ]
        if len(rows) > limit or len(found) <= limit:
            break
        last = found[-1]
        assert last.published_at is not None
        batch = query.where(paging.beyond(model.published_at, model.id, last.published_at, last.id))
    more = len(rows) > limit
    rows = rows[:limit]
    tenants: dict[uuid.UUID, TenantPublic] = {}
    items = []
    for row in rows:
        if row.tenant_id not in tenants:
            tenants[row.tenant_id] = await _tenant(db, row.tenant_id)
        assert row.published_at is not None
        items.append(
            Entry(
                did=row.identity.did or "",
                name=row.name,
                description=None if isinstance(row, Mediator) else row.description,
                url=row.url if isinstance(row, Mediator) else None,
                slug=row.slug if isinstance(row, Issuer) else None,
                credential_types=row.credential_types if isinstance(row, Issuer) else None,
                offers=[t for t in row.credential_types if t in (row.request_forms or {})]
                if isinstance(row, Issuer)
                else None,
                published_at=row.published_at,
                tenant=tenants[row.tenant_id],
            )
        )
    last_row = rows[-1] if rows else None
    return CatalogPage(
        items=items,
        next_cursor=paging.encode(last_row.published_at, last_row.id)
        if more and last_row
        else None,
    )
