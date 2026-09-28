"""What a tenant holds: its issuers, verifiers, mediators and identities.

Identities are the tenant's register of DIDs. The tenant and every issuer,
verifier and mediator act as one of their own, created with them and named like
them; each publishes its DID document (see `registry_api.dids`). Mediators are
where messages arrive: issuers and verifiers pick one of the tenant's.

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

from registry_api import dids, mediators
from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.config import get_settings
from registry_api.models import Identity, Issuer, Mediator, Tenant, TenantMember, Verifier

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["directory"])

Item = Issuer | Verifier | Mediator | Identity


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
    # One of the tenant's mediators, to receive messages through; optional.
    mediator_id: uuid.UUID | None = None

    @field_validator("description")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return _clean(value)


class MediatorIn(NamedIn):
    # Where it listens: https (plain http only on loopback).
    url: str = Field(min_length=1, max_length=2048)


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


class IdentityRef(BaseModel):
    id: uuid.UUID
    name: str


class MediatorRef(BaseModel):
    id: uuid.UUID
    name: str


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
    # Mediators: where they listen.
    url: str | None = None
    # Identities: the tenant, issuers and verifiers that act as them.
    used_by: list[Use] | None = None
    # Issuers, verifiers and mediators: when published; `null` while a draft.
    published_at: datetime | None = None


class IdentityDetail(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    did: str
    # The tenant, issuer or verifier that acts as it.
    used_by: list[Use]
    # What `did` resolves to, and the URL it is published at.
    document: dict[str, Any]
    document_url: str
    # Whether `document_url` answers: not while what acts as it is a draft.
    published: bool


class ItemDetail(ItemOut):
    """An issuer, verifier or mediator, opened on its own: with its DID, the
    document it resolves to and where that is published (a draft's is not)."""

    did: str
    document: dict[str, Any]
    document_url: str


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
        mediator = MediatorRef(id=item.mediator.id, name=item.mediator.name)
    return ItemOut(
        id=item.id,
        name=item.name,
        description=getattr(item, "description", None),
        created_at=item.created_at,
        identity=identity,
        mediator=mediator,
        url=item.url if isinstance(item, Mediator) else None,
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


async def tenant_mediator(
    db: DbSession, tenant_id: uuid.UUID, mediator_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The mediator chosen, if it is one of the tenant's."""
    if mediator_id is None:
        return None
    mediator = await db.get(Mediator, mediator_id)
    if mediator is None or mediator.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="mediator_not_found")
    return mediator.id


def _endpoint(url: str) -> str:
    try:
        return mediators.endpoint(url)
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
    responses={422: {"description": "`mediator_not_found`: not one of the tenant's"}},
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
    responses={422: {"description": "`mediator_not_found`: not one of the tenant's"}},
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


async def item_detail(db: DbSession, item: Issuer | Verifier | Mediator) -> ItemDetail:
    did_url = get_settings().did_url
    return ItemDetail(
        **_out(item).model_dump(),
        did=dids.did_for(did_url, item.identity.slug),
        document=await dids.document_for(db, did_url, item.identity),
        document_url=dids.url_for(did_url, item.identity.slug),
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
        422: {"description": "`mediator_invalid`, `mediator_insecure` (plain off loopback)"}
    },
)
async def create_mediator(tenant_id: TenantId, body: MediatorIn, db: DbSession) -> ItemOut:
    url = _endpoint(body.url)
    identity_id = await _identity_for(db, tenant_id, body.name)
    return await _create(
        db, Mediator(tenant_id=tenant_id, name=body.name, url=url, identity_id=identity_id)
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
    summary="Change a mediator's name or address (its DID stays)",
    responses={422: {"description": "`name_required`, `mediator_invalid`, `mediator_insecure`"}},
)
async def update_mediator(
    tenant_id: TenantId, mediator_id: uuid.UUID, body: MediatorPatch, db: DbSession
) -> ItemDetail:
    mediator = await _mediator(db, tenant_id, mediator_id)
    sent = body.model_fields_set
    if "url" in sent:
        mediator.url = _endpoint(body.url or "")
    if "name" in sent:
        _rename(mediator, body.name)
    await db.commit()
    await db.refresh(mediator, ["identity"])
    return await item_detail(db, mediator)


@router.get("/identities", summary="The tenant's identities, newest first")
async def list_identities(
    tenant_id: TenantId, db: DbSession, limit: Limit = 20, cursor: Cursor = None
) -> Page:
    return await _page(db, Identity, tenant_id, limit, cursor)


@router.get("/identities/{identity_id}", summary="One identity, with its DID document")
async def get_identity(
    tenant_id: TenantId, identity_id: uuid.UUID, db: DbSession
) -> IdentityDetail:
    identity = await db.get(Identity, identity_id)
    if identity is None or identity.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="identity_not_found")
    did_url = get_settings().did_url
    if await dids.is_root_identity(db, identity.id):
        did, document_url = dids.domain_did(did_url), dids.domain_url(did_url)
    else:
        did, document_url = (
            dids.did_for(did_url, identity.slug),
            dids.url_for(did_url, identity.slug),
        )
    return IdentityDetail(
        id=identity.id,
        name=identity.name,
        created_at=identity.created_at,
        did=did,
        used_by=(await _uses(db, [identity.id]))[identity.id],
        document=await dids.document_for(db, did_url, identity),
        document_url=document_url,
        published=not await dids.is_draft(db, identity.id),
    )


@router.post("/identities", status_code=status.HTTP_201_CREATED, summary="Register an identity")
async def create_identity(tenant_id: TenantId, body: NamedIn, db: DbSession) -> ItemOut:
    return await _create(db, Identity(tenant_id=tenant_id, name=body.name))
