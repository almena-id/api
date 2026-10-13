"""What is done to an issuer, verifier or mediator: publish (whoever signs as
the tenant), unpublish and delete (admins).

Publishing is what makes one visible outside its tenant.

Each starts as a draft, seen only by the tenant's members. **Publishing is
endorsing**: whoever signs as the tenant signs from a wallet the tenant's membership credential
for it and, in the same approval, its `whois.vp` (see `credentials` and
`wallet.py`); once both are checked it is published — its DID resolves, the
catalogue lists it. Under the flow `none` the platform signs both when an
admin publishes, at once (`custody`). Unpublishing takes all of it back.

The catalogue — and the endorsement — name the tenant by its DID, never by
the tenant's own name, which starts as "Tenant of {email}".

Deleting one deletes its identity too (it is its own, never shared): its DID
stops resolving for good, and an issuer's or verifier's keys leave the vault.
A mediator's users are left without one.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update

from registry_api import credentials, custody, dids, messaging_keys, paging, signing_flows
from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.api.routes.directory import ItemDetail, TenantId, item_detail
from registry_api.api.routes.members import admin_of
from registry_api.api.routes.wallet import (
    LocaleIn,
    RequestOut,
    SignedOut,
    SignObject,
    endorse,
    my_keys,
    new_sign_request,
)
from registry_api.broker import Broker, BrokerError, get_broker
from registry_api.models import Identity, Issuer, Mediator, Tenant, Verifier
from registry_api.trust_anchor import PREFIX
from registry_api.vault import Vault, VaultError, get_vault

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
    "or, under the flow `none`, both signed by the platform at once "
    '(`{status: "signed"}`); publishing again renews it',
    responses={
        403: {"description": "`not_a_signer`: no wallet of theirs signs for it"},
        409: {
            "description": "`identity_pending`: its identity is not signed yet; "
            "`identity_outdated`: its published document does not name the admin's key yet; "
            "`tenant_pending`: the tenant's DID is not signed yet; "
            "`tenant_outdated`: under `none`, the tenant's DID or the item's does not name "
            "the platform's key yet (the old flow's signers hand over first)"
        },
        503: {"description": "`vault_unavailable`: the platform could not sign"},
    },
)
async def publish(
    tenant_id: TenantId,
    kind: Kind,
    item_id: uuid.UUID,
    body: LocaleIn,
    session: CurrentSession,
    db: DbSession,
    vault: Annotated[Vault, Depends(get_vault)],
) -> RequestOut | SignedOut:
    item = await _owned(db, kind, tenant_id, item_id)
    identity = await db.get_one(Identity, item.identity_id)
    tenant = await db.get_one(Tenant, tenant_id)
    # Who the flow names, before anything about the item: a member it does
    # not name is told so, whatever state the item is in.
    if not await signing_flows.signs(db, tenant, session.user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_a_signer")
    # Under `none` whatever waits to be signed is, first: the platform signs it.
    if tenant.signing_flow == "none":
        await custody.settle(db, vault, tenant_id)
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
    # Under `none` the platform's key is the one.
    platform = tenant.platform_key if tenant.signing_flow == "none" else None
    mine = {platform} if platform else set(await my_keys(db, session.user_id))
    if not mine & set(signers):
        # Moving to `none`, the tenant's DID still names the old flow's keys.
        if platform:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="tenant_outdated")
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
    if platform:
        try:
            signed = {**credential, "proof": await _signed(vault, tenant, request)}
            presentation = {**(request.presentation or {}), "verifiableCredential": [signed]}
            presented = await custody.proof(
                vault, tenant, presentation, owner.did, "authentication"
            )
        except VaultError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="vault_unavailable"
            ) from None
        await endorse(db, extra, request, signed, presented, f"did:key:{platform}")
        await db.commit()
        return SignedOut()
    return await new_sign_request(db, session.user_id, body, request, extra)


async def _signed(vault: Vault, tenant: Tenant, request: SignObject) -> dict[str, Any]:
    return await custody.proof(
        vault, tenant, request.document, request.verification_method, request.proof_purpose
    )


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
    vault: Annotated[Vault, Depends(get_vault)],
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
    # A mediator's users name it no more: under `none` that is signed now.
    if isinstance(item, Mediator):
        await custody.settle(db, vault, tenant_id)
    # After the commit: a key left behind by a vault that failed is harmless
    # (nothing names it any more); an item left without its key is not.
    if isinstance(item, Issuer | Verifier):
        try:
            await messaging_keys.forget(vault, tenant_id, messaging_keys.kind_of(item), item.id)
        except VaultError:
            logger.warning("vault_keys_left", extra={"kind": kind, "item_id": str(item.id)})
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
