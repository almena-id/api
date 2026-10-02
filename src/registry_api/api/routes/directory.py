"""What a tenant holds: its issuers, verifiers, mediators and identities.

Identities are the tenant's register of DIDs. The tenant and every issuer,
verifier and mediator act as one of their own, created with them and named like
them; each publishes its DID document (see `registry_api.dids`). Mediators are
where messages arrive: issuers and verifiers pick one of the tenant's, or a
public one of another tenant's once published (`mediator-choices`).

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
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import dids, logs, mediators
from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.config import get_settings
from registry_api.models import (
    Identity,
    Issuer,
    Mediator,
    Tenant,
    TenantDomain,
    TenantMember,
    Verifier,
)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["directory"])

Item = Issuer | Verifier | Mediator | Identity


async def member_tenant(tenant_id: uuid.UUID, session: CurrentSession, db: DbSession) -> uuid.UUID:
    """The tenant in the path, if the signed-in user belongs to it."""
    member = await db.get(TenantMember, (tenant_id, session.user_id))
    if member is None:
        # Not "forbidden": whether somebody else's tenant exists is not ours to say.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="tenant_not_found")
    logs.set_tenant(tenant_id)
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
    # One of the tenant's mediators, or a published public one, to receive
    # messages through; optional.
    mediator_id: uuid.UUID | None = None

    @field_validator("description")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return _clean(value)


class MediatorIn(NamedIn):
    # Where it listens: `https://{subdomain}.{domain}`, the domain one of the
    # tenant's verified ones.
    subdomain: str = Field(min_length=1, max_length=200)
    domain_id: uuid.UUID
    # Offered to every tenant (once published), not only this one.
    public: bool = False


class DescribedPatch(BaseModel):
    # Each field is changed only when sent; `description` or `mediator_id`
    # sent as `null` removes it.
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    mediator_id: uuid.UUID | None = None


class MediatorPatch(BaseModel):
    # Each field is changed only when sent.
    name: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=2048)
    public: bool | None = None


class IdentityRef(BaseModel):
    id: uuid.UUID
    name: str


class MediatorRef(BaseModel):
    id: uuid.UUID
    name: str
    # Whether it is the tenant's own; otherwise another tenant's public one.
    own: bool = True


class Use(BaseModel):
    kind: Literal["tenant", "issuer", "verifier", "mediator"]
    id: uuid.UUID
    name: str


class ItemOut(BaseModel):
    id: uuid.UUID
    name: str
    # Issuers and verifiers carry one; identities and mediators do not.
    description: str | None = None
    created_at: datetime
    # Issuers, verifiers and mediators: the identity they act as.
    identity: IdentityRef | None = None
    # Issuers and verifiers: the mediator they receive messages through.
    mediator: MediatorRef | None = None
    # Mediators: where they listen, and whether they are offered to every tenant.
    url: str | None = None
    public: bool | None = None
    # Identities: the tenant, issuers and verifiers that act as them.
    used_by: list[Use] | None = None
    # Issuers, verifiers and mediators: when published; `null` while a draft.
    published_at: datetime | None = None
    # In lists: where the DID's signature stands (its identity's, or its own).
    signature: dids.Status | None = None


class Signed(BaseModel):
    """Where an identity's DID stands: its did:webvh DID once its first log
    entry is signed (`null` while pending); `signature` says whether what it
    should publish (`document`) is what it last signed; the URLs where its log
    and its did:web document are served (`null` while pending)."""

    did: str | None
    signature: dids.Status
    document: dict[str, Any]
    # What the log says now, as signed; `null` while pending.
    signed_document: dict[str, Any] | None
    log_url: str | None
    document_url: str | None
    # A published issuer's, verifier's or mediator's endorsement by its
    # tenant: where its `whois.vp` is, and until when it holds.
    whois_url: str | None = None
    endorsed_until: datetime | None = None


class IdentityDetail(Signed):
    id: uuid.UUID
    name: str
    created_at: datetime
    # The tenant, issuer or verifier that acts as it.
    used_by: list[Use]
    # Whether the URLs answer: not while what acts as it is a draft.
    published: bool


class ItemDetail(Signed, ItemOut):
    """An issuer, verifier or mediator, opened on its own: with its identity's
    DID, the document it should publish and where it is (a draft's is not)."""


async def signed_of(db: AsyncSession, identity: Identity) -> Signed:
    did_url = get_settings().did_url
    base = dids.base_url(did_url, identity.slug, await dids.is_root_identity(db, identity.id))
    return Signed(
        did=identity.did,
        signature=await dids.status_of(db, did_url, identity),
        document=await dids.desired(db, did_url, identity),
        signed_document=await dids.published(db, identity),
        log_url=f"{base}/did.jsonl" if identity.did else None,
        document_url=f"{base}/did.json" if identity.did else None,
        whois_url=f"{base}/whois.vp" if identity.presentation else None,
        endorsed_until=identity.endorsed_until,
    )


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
    identity = mediator = None
    if isinstance(item, Issuer | Verifier | Mediator):
        identity = IdentityRef(id=item.identity.id, name=item.identity.name)
    if isinstance(item, Issuer | Verifier) and item.mediator is not None:
        mediator = MediatorRef(
            id=item.mediator.id,
            name=item.mediator.name,
            own=item.mediator.tenant_id == item.tenant_id,
        )
    return ItemOut(
        id=item.id,
        name=item.name,
        description=getattr(item, "description", None),
        created_at=item.created_at,
        identity=identity,
        mediator=mediator,
        url=item.url if isinstance(item, Mediator) else None,
        public=item.public if isinstance(item, Mediator) else None,
        used_by=used_by,
        published_at=getattr(item, "published_at", None),
    )


async def _uses(db: DbSession, identity_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Use]]:
    """For each identity, the tenant, issuer, verifier or mediator that acts as it."""
    uses: dict[uuid.UUID, list[Use]] = {i: [] for i in identity_ids}
    tenants = await db.execute(
        select(Tenant.identity_id, Tenant.id, Tenant.name).where(
            Tenant.identity_id.in_(identity_ids)
        )
    )
    for identity_id, tenant_id, name in tenants:
        if identity_id is not None:
            uses[identity_id].append(Use(kind="tenant", id=tenant_id, name=name or ""))
    for kind, model in (("issuer", Issuer), ("verifier", Verifier), ("mediator", Mediator)):
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
    did_url = get_settings().did_url
    items = []
    for row in rows:
        item = _out(row, uses.get(row.id) if model is Identity else None)
        identity = row if isinstance(row, Identity) else row.identity
        item.signature = await dids.status_of(db, did_url, identity)
        items.append(item)
    return Page(
        items=items,
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
        await db.refresh(item, ["identity", "mediator"])
    elif isinstance(item, Mediator):
        await db.refresh(item, ["identity"])
    return _out(item, [] if isinstance(item, Identity) else None)


async def _identity_for(db: DbSession, tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    """A new identity for an issuer, verifier or mediator, named like it."""
    identity = Identity(tenant_id=tenant_id, name=name, created_at=datetime.now(UTC))
    db.add(identity)
    await db.flush()
    return identity.id


def offered(mediator: Mediator) -> bool:
    """Whether another tenant's mediator may be picked: public and published."""
    return mediator.public and mediator.published_at is not None


async def tenant_mediator(
    db: DbSession, tenant_id: uuid.UUID, mediator_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The mediator chosen, if it is one of the tenant's or a published public one."""
    if mediator_id is None:
        return None
    mediator = await db.get(Mediator, mediator_id)
    if mediator is None or not (mediator.tenant_id == tenant_id or offered(mediator)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="mediator_not_found")
    return mediator.id


def _endpoint(url: str) -> str:
    try:
        return mediators.endpoint(url)
    except mediators.MediatorError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=error.code) from None


async def _address(
    db: DbSession, tenant_id: uuid.UUID, subdomain: str, domain_id: uuid.UUID
) -> str:
    """A new mediator's address: the subdomain typed of one of the tenant's
    verified domains."""
    domain = await db.get(TenantDomain, domain_id)
    if domain is None or domain.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="domain_not_found")
    if domain.verified_at is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="domain_unverified")
    try:
        return mediators.address(subdomain, domain.domain)
    except mediators.MediatorError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=error.code) from None


@router.get("/issuers", summary="The tenant's issuers, newest first")
async def list_issuers(
    tenant_id: TenantId, db: DbSession, limit: Limit = 20, cursor: Cursor = None
) -> Page:
    return await _page(db, Issuer, tenant_id, limit, cursor)


@router.post(
    "/issuers",
    status_code=status.HTTP_201_CREATED,
    summary="Register an issuer",
    responses={422: {"description": "`mediator_not_found`: neither the tenant's nor a public one"}},
)
async def create_issuer(tenant_id: TenantId, body: DescribedIn, db: DbSession) -> ItemOut:
    mediator_id = await tenant_mediator(db, tenant_id, body.mediator_id)
    identity_id = await _identity_for(db, tenant_id, body.name)
    return await _create(
        db,
        Issuer(
            tenant_id=tenant_id,
            name=body.name,
            description=body.description,
            identity_id=identity_id,
            mediator_id=mediator_id,
        ),
    )


@router.get("/verifiers", summary="The tenant's verifiers, newest first")
async def list_verifiers(
    tenant_id: TenantId, db: DbSession, limit: Limit = 20, cursor: Cursor = None
) -> Page:
    return await _page(db, Verifier, tenant_id, limit, cursor)


@router.post(
    "/verifiers",
    status_code=status.HTTP_201_CREATED,
    summary="Register a verifier",
    responses={422: {"description": "`mediator_not_found`: neither the tenant's nor a public one"}},
)
async def create_verifier(tenant_id: TenantId, body: DescribedIn, db: DbSession) -> ItemOut:
    mediator_id = await tenant_mediator(db, tenant_id, body.mediator_id)
    identity_id = await _identity_for(db, tenant_id, body.name)
    return await _create(
        db,
        Verifier(
            tenant_id=tenant_id,
            name=body.name,
            description=body.description,
            identity_id=identity_id,
            mediator_id=mediator_id,
        ),
    )


async def _described(
    db: DbSession, model: type[Issuer | Verifier], tenant_id: uuid.UUID, item_id: uuid.UUID
) -> Issuer | Verifier:
    item = cast(Issuer | Verifier | None, await db.get(model, item_id))
    if item is None or item.tenant_id != tenant_id:
        kind = "issuer" if model is Issuer else "verifier"
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{kind}_not_found")
    return item


async def item_detail(db: AsyncSession, item: Issuer | Verifier | Mediator) -> ItemDetail:
    return ItemDetail(
        **_out(item).model_dump(exclude={"signature"}),
        **(await signed_of(db, item.identity)).model_dump(),
    )


def _rename(item: Issuer | Verifier | Mediator, name: str | None) -> None:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="name_required")
    # Its identity is named like it, and stays so unless renamed on its own.
    if item.identity.name == item.name:
        item.identity.name = name
    item.name = name


async def _update_described(
    db: DbSession, item: Issuer | Verifier, tenant_id: uuid.UUID, body: DescribedPatch
) -> ItemDetail:
    sent = body.model_fields_set
    if "mediator_id" in sent:
        item.mediator_id = await tenant_mediator(db, tenant_id, body.mediator_id)
    if "description" in sent:
        item.description = _clean(body.description)
    if "name" in sent:
        _rename(item, body.name)
    await db.commit()
    await db.refresh(item, ["identity", "mediator"])
    return await item_detail(db, item)


@router.get("/issuers/{issuer_id}", summary="One issuer, with its DID")
async def get_issuer(tenant_id: TenantId, issuer_id: uuid.UUID, db: DbSession) -> ItemDetail:
    return await item_detail(db, await _described(db, Issuer, tenant_id, issuer_id))


@router.patch(
    "/issuers/{issuer_id}",
    summary="Change an issuer's name, description or mediator (its DID stays)",
    responses={422: {"description": "`name_required`, `mediator_not_found`"}},
)
async def update_issuer(
    tenant_id: TenantId, issuer_id: uuid.UUID, body: DescribedPatch, db: DbSession
) -> ItemDetail:
    issuer = await _described(db, Issuer, tenant_id, issuer_id)
    return await _update_described(db, issuer, tenant_id, body)


@router.get("/verifiers/{verifier_id}", summary="One verifier, with its DID")
async def get_verifier(tenant_id: TenantId, verifier_id: uuid.UUID, db: DbSession) -> ItemDetail:
    return await item_detail(db, await _described(db, Verifier, tenant_id, verifier_id))


@router.patch(
    "/verifiers/{verifier_id}",
    summary="Change a verifier's name, description or mediator (its DID stays)",
    responses={422: {"description": "`name_required`, `mediator_not_found`"}},
)
async def update_verifier(
    tenant_id: TenantId, verifier_id: uuid.UUID, body: DescribedPatch, db: DbSession
) -> ItemDetail:
    verifier = await _described(db, Verifier, tenant_id, verifier_id)
    return await _update_described(db, verifier, tenant_id, body)


@router.get("/mediators", summary="The tenant's mediators, newest first")
async def list_mediators(
    tenant_id: TenantId, db: DbSession, limit: Limit = 20, cursor: Cursor = None
) -> Page:
    return await _page(db, Mediator, tenant_id, limit, cursor)


@router.post(
    "/mediators",
    status_code=status.HTTP_201_CREATED,
    summary="Register a mediator; the registry gives it its identity",
    responses={
        422: {
            "description": "`domain_not_found` (not the tenant's), `domain_unverified`, "
            "`subdomain_invalid`"
        }
    },
)
async def create_mediator(tenant_id: TenantId, body: MediatorIn, db: DbSession) -> ItemOut:
    url = await _address(db, tenant_id, body.subdomain, body.domain_id)
    identity_id = await _identity_for(db, tenant_id, body.name)
    return await _create(
        db,
        Mediator(
            tenant_id=tenant_id,
            name=body.name,
            url=url,
            public=body.public,
            identity_id=identity_id,
        ),
    )


async def _mediator(db: DbSession, tenant_id: uuid.UUID, mediator_id: uuid.UUID) -> Mediator:
    mediator = await db.get(Mediator, mediator_id)
    if mediator is None or mediator.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="mediator_not_found")
    return mediator


@router.get("/mediators/{mediator_id}", summary="One mediator, with its DID")
async def get_mediator(tenant_id: TenantId, mediator_id: uuid.UUID, db: DbSession) -> ItemDetail:
    return await item_detail(db, await _mediator(db, tenant_id, mediator_id))


@router.patch(
    "/mediators/{mediator_id}",
    summary="Change a mediator's name, address or whether it is public (its DID stays)",
    responses={422: {"description": "`name_required`, `mediator_invalid`, `mediator_insecure`"}},
)
async def update_mediator(
    tenant_id: TenantId, mediator_id: uuid.UUID, body: MediatorPatch, db: DbSession
) -> ItemDetail:
    mediator = await _mediator(db, tenant_id, mediator_id)
    sent = body.model_fields_set
    if "url" in sent:
        mediator.url = _endpoint(body.url or "")
    # Tenants that picked it while public keep it: it is only no longer offered.
    if body.public is not None:
        mediator.public = body.public
    if "name" in sent:
        _rename(mediator, body.name)
    await db.commit()
    await db.refresh(mediator, ["identity"])
    return await item_detail(db, mediator)


class MediatorChoice(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    # The tenant's own (drafts included), or another tenant's public one.
    own: bool
    published: bool


@router.get(
    "/mediator-choices",
    summary="The mediators the tenant may pick: its own, then the published public ones",
)
async def mediator_choices(tenant_id: TenantId, db: DbSession) -> list[MediatorChoice]:
    # A hundred of each is more than a tenant is expected to choose from.
    own = await db.scalars(
        select(Mediator)
        .where(Mediator.tenant_id == tenant_id)
        .order_by(Mediator.created_at, Mediator.id)
        .limit(100)
    )
    public = await db.scalars(
        select(Mediator)
        .where(
            Mediator.tenant_id != tenant_id,
            Mediator.public,
            Mediator.published_at.is_not(None),
        )
        .order_by(Mediator.name, Mediator.id)
        .limit(100)
    )
    return [
        MediatorChoice(
            id=m.id,
            name=m.name,
            url=m.url,
            own=m.tenant_id == tenant_id,
            published=m.published_at is not None,
        )
        for m in [*own, *public]
    ]


@router.get("/identities", summary="The tenant's identities, newest first")
async def list_identities(
    tenant_id: TenantId, db: DbSession, limit: Limit = 20, cursor: Cursor = None
) -> Page:
    return await _page(db, Identity, tenant_id, limit, cursor)


class Waiting(BaseModel):
    id: uuid.UUID
    name: str
    signature: dids.Status


@router.get(
    "/signatures",
    summary="The tenant's identities whose DID waits for a signature (pending or outdated)",
)
async def waiting_signatures(tenant_id: TenantId, db: DbSession) -> list[Waiting]:
    did_url = get_settings().did_url
    identities = await db.scalars(
        select(Identity)
        .where(Identity.tenant_id == tenant_id)
        .order_by(Identity.created_at, Identity.id)
    )
    waiting = []
    for identity in identities:
        state = await dids.status_of(db, did_url, identity)
        if state != "signed":
            waiting.append(Waiting(id=identity.id, name=identity.name, signature=state))
    return waiting


@router.get("/identities/{identity_id}", summary="One identity, with its DID document")
async def get_identity(
    tenant_id: TenantId, identity_id: uuid.UUID, db: DbSession
) -> IdentityDetail:
    identity = await db.get(Identity, identity_id)
    if identity is None or identity.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="identity_not_found")
    return IdentityDetail(
        id=identity.id,
        name=identity.name,
        created_at=identity.created_at,
        used_by=(await _uses(db, [identity.id]))[identity.id],
        published=not await dids.is_draft(db, identity.id),
        **(await signed_of(db, identity)).model_dump(),
    )


@router.post("/identities", status_code=status.HTTP_201_CREATED, summary="Register an identity")
async def create_identity(tenant_id: TenantId, body: NamedIn, db: DbSession) -> ItemOut:
    return await _create(db, Identity(tenant_id=tenant_id, name=body.name))
