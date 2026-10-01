"""What is done to an issuer, verifier or mediator: publish (whoever signs as
the tenant), unpublish and delete (admins).

Publishing is what makes one visible outside its tenant.

Each starts as a draft, seen only by the tenant's members. **Publishing is
endorsing**: whoever signs as the tenant signs from a wallet the tenant's membership credential
for it and, in the same approval, its `whois.vp` (see `credentials` and
`wallet.py`); once both are checked it is published — its DID resolves, the
catalogue lists it. Unpublishing takes all of it back.

The catalogue — and the endorsement — name the tenant by its DID, never by
the tenant's own name, which starts as "Tenant of {email}".

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

from registry_api import credentials, dids, signing_flows
from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.api.routes.directory import ItemDetail, TenantId, item_detail
from registry_api.api.routes.members import admin_of
from registry_api.api.routes.wallet import (
    LocaleIn,
    RequestOut,
    SignObject,
    my_keys,
    new_sign_request,
)
from registry_api.models import Identity, Issuer, Mediator, Tenant, Verifier

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
    identity = await db.get_one(Identity, item.identity_id)
    identity.presentation = None
    identity.endorsed_until = None
    await db.commit()
    await db.refresh(
        item, ["identity", "mediator"] if not isinstance(item, Mediator) else ["identity"]
    )
    return await item_detail(db, item)


@router.post(
    "/{kind}/{item_id}/publish",
    summary="Publish an issuer, verifier or mediator by endorsing it: a request for the "
    "signer's wallet to sign the tenant's membership credential and the item's whois.vp "
    "(publishing again renews it)",
    responses={
        403: {"description": "`not_a_signer`: no wallet of theirs signs for it"},
        409: {
            "description": "`identity_pending`: its identity is not signed yet; "
            "`identity_outdated`: its published document does not name the admin's key yet; "
            "`tenant_pending`: the tenant's DID is not signed yet"
        },
    },
)
async def publish(
    tenant_id: TenantId,
    kind: Kind,
    item_id: uuid.UUID,
    body: LocaleIn,
    session: CurrentSession,
    db: DbSession,
) -> RequestOut:
    item = await _owned(db, kind, tenant_id, item_id)
    identity = await db.get_one(Identity, item.identity_id)
    tenant = await db.get_one(Tenant, tenant_id)
    # Who the flow names, before anything about the item: a member it does
    # not name is told so, whatever state the item is in.
    if not await signing_flows.signs(db, tenant, session.user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_a_signer")
    owner = await db.get(Identity, tenant.identity_id) if tenant.identity_id else None
    # Nothing is published under a DID that does not exist yet.
    if identity.did is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="identity_pending")
    if owner is None or owner.did is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="tenant_pending")
    # The key must sign for the tenant (its published `assertionMethod`) and
    # for the item as its controller (the item's published `authentication`).
    tenant_keys = set(dids.keys_under(await dids.published(db, owner), "assertionMethod"))
    item_doc = await dids.published(db, identity) or {}
    controlling = {
        str(ref).removeprefix(f"{owner.did}#")
        for ref in item_doc.get("authentication", [])
        if str(ref).startswith(f"{owner.did}#")
    }
    signers = sorted(tenant_keys & controlling)
    mine = set(await my_keys(db, session.user_id))
    if not mine & set(signers):
        if mine & tenant_keys:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="identity_outdated")
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_a_signer")
    credential = credentials.membership(
        issuer=owner.did,
        subject=identity.did,
        role=kind[:-1],
        name=item.name,
        now=datetime.now(UTC),
    )
    request = SignObject(
        kind="endorsement",
        identity=item.name,
        tenant=tenant.name,
        did=identity.did,
        valid_until=credential["validUntil"],
        signers=signers,
        verification_method=owner.did,
        document=credential,
        presentation={
            "@context": [credentials.CONTEXT],
            "type": ["VerifiablePresentation"],
            "holder": identity.did,
            "verifiableCredential": [],
        },
    )
    extra = {"kind": kind, "item_id": str(item.id), "identity_id": str(identity.id)}
    return await new_sign_request(db, session.user_id, body.locale, request, extra)


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


class Entry(BaseModel):
    did: str
    name: str
    # Issuers and verifiers carry one; mediators do not.
    description: str | None = None
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


async def _tenant(db: DbSession, tenant_id: uuid.UUID) -> TenantPublic:
    tenant = await db.get(Tenant, tenant_id)
    owner = await db.get(Identity, tenant.identity_id) if tenant and tenant.identity_id else None
    return TenantPublic(did=owner.did or "" if owner else "")


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
    # Only what resolves: published, under a DID that has been signed.
    query = (
        select(model)
        .join(Identity, Identity.id == model.identity_id)
        .where(model.published_at.is_not(None), Identity.did.is_not(None))
    )
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
    return CatalogPage(items=items, next_cursor=_encode(rows[-1]) if more else None)
