"""What admins do to an issuer, verifier or mediator: publish, unpublish, delete.

Publishing is what makes one visible outside its tenant.

Each starts as a draft, seen only by the tenant's members. Publishing it (admins
only) makes its DID resolve and lists it in the public catalogue; unpublishing
takes both back. Certification is not required: the catalogue says whether the
tenant is certified, and whoever reads it decides what that is worth.

The catalogue names the tenant by its DID and, once certified, by its legal
name; never by the tenant's own name, which starts as "Tenant of {email}".

Deleting one deletes its identity too (it is its own, never shared): its DID
stops resolving for good. A mediator's users are left without one.
"""

import base64
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import and_, or_, select, update

from registry_api import dids
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import ItemDetail, item_detail
from registry_api.api.routes.members import admin_of
from registry_api.config import get_settings
from registry_api.models import Certification, Identity, Issuer, Mediator, Tenant, Verifier

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


async def _set(db: DbSession, item: Publishable, published: bool) -> ItemDetail:
    if published and item.published_at is None:
        item.published_at = datetime.now(UTC)
    elif not published:
        item.published_at = None
    await db.commit()
    await db.refresh(
        item, ["identity", "mediator"] if not isinstance(item, Mediator) else ["identity"]
    )
    return await item_detail(db, item)


@router.post(
    "/{kind}/{item_id}/publish",
    summary="Publish an issuer, verifier or mediator: its DID resolves and the catalogue lists it",
    responses={403: {"description": "`not_admin`"}},
)
async def publish(
    tenant_id: AdminTenant, kind: Kind, item_id: uuid.UUID, db: DbSession
) -> ItemDetail:
    return await _set(db, await _owned(db, kind, tenant_id, item_id), True)


@router.post(
    "/{kind}/{item_id}/unpublish",
    summary="Take one back to a draft: its DID stops resolving, the catalogue drops it",
    responses={403: {"description": "`not_admin`"}},
)
async def unpublish(
    tenant_id: AdminTenant, kind: Kind, item_id: uuid.UUID, db: DbSession
) -> ItemDetail:
    return await _set(db, await _owned(db, kind, tenant_id, item_id), False)


@router.delete(
    "/{kind}/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an issuer, verifier or mediator, and its identity",
    responses={403: {"description": "`not_admin`"}},
)
async def delete(tenant_id: AdminTenant, kind: Kind, item_id: uuid.UUID, db: DbSession) -> Response:
    item = await _owned(db, kind, tenant_id, item_id)
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
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class TenantPublic(BaseModel):
    did: str
    certified: bool
    # The legal name Almena approved; `null` while uncertified.
    legal_name: str | None


class Entry(BaseModel):
    did: str
    name: str
    # Issuers and verifiers carry one; mediators do not.
    description: str | None = None
    # Mediators: where they listen.
    url: str | None = None
    published_at: datetime
    tenant: TenantPublic


class CatalogPage(BaseModel):
    items: list[Entry]
    # Pass it back as `cursor` for the next page; `null` on the last one.
    next_cursor: str | None


def _encode(item: Publishable) -> str:
    assert item.published_at is not None
    raw = f"{item.published_at.isoformat()}|{item.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        moment, item_id = raw.split("|")
        return datetime.fromisoformat(moment), uuid.UUID(item_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_cursor") from None


async def _tenant(db: DbSession, did_url: str, tenant_id: uuid.UUID) -> TenantPublic:
    tenant = await db.get(Tenant, tenant_id)
    owner = await db.get(Identity, tenant.identity_id) if tenant and tenant.identity_id else None
    legal_name = await db.scalar(
        select(Certification.legal_name).where(
            Certification.tenant_id == tenant_id, Certification.status == "approved"
        )
    )
    return TenantPublic(
        did=await dids.did_of(db, did_url, owner) if owner else "",
        certified=legal_name is not None,
        legal_name=legal_name,
    )


@catalog.get(
    "/{kind}", summary="Public: the published issuers, verifiers or mediators, newest first"
)
async def list_published(
    kind: Kind,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> CatalogPage:
    model = MODELS[kind]
    query = select(model).where(model.published_at.is_not(None))
    if cursor:
        moment, item_id = _decode(cursor)
        query = query.where(
            or_(
                model.published_at < moment,
                and_(model.published_at == moment, model.id < item_id),
            )
        )
    rows = cast(
        list[Publishable],
        list(
            await db.scalars(
                query.order_by(model.published_at.desc(), model.id.desc()).limit(limit + 1)
            )
        ),
    )
    more = len(rows) > limit
    rows = rows[:limit]
    did_url = get_settings().did_url
    tenants: dict[uuid.UUID, TenantPublic] = {}
    items = []
    for row in rows:
        if row.tenant_id not in tenants:
            tenants[row.tenant_id] = await _tenant(db, did_url, row.tenant_id)
        assert row.published_at is not None
        items.append(
            Entry(
                did=dids.did_for(did_url, row.identity.slug),
                name=row.name,
                description=None if isinstance(row, Mediator) else row.description,
                url=row.url if isinstance(row, Mediator) else None,
                published_at=row.published_at,
                tenant=tenants[row.tenant_id],
            )
        )
    return CatalogPage(items=items, next_cursor=_encode(rows[-1]) if more else None)
