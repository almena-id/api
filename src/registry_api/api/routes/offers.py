"""The offers: a published issuer's credential types that have a request form
(`issuer_credentials`), listed in the public catalogue (``GET /catalog/offers``)
and opened one by one (``GET /catalog/issuers/{slug}/offers/{type}``); holders
apply for them through the catalog (`routes.applications`)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    status,
)
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import answers as checks
from registry_api import credential_catalog as credentials
from registry_api import field_catalog as catalog
from registry_api import (
    form_credentials,
    paging,
    trust_anchor,
)
from registry_api.api.routes.auth import DbSession
from registry_api.models import (
    Form,
    Identity,
    Issuer,
)

router = APIRouter(prefix="/catalog", tags=["applications"])

# The offer -------------------------------------------------------------------


async def offering(db: AsyncSession, slug: str, type_id: str) -> tuple[Issuer, Form]:
    issuer = await db.scalar(select(Issuer).where(Issuer.slug == slug))
    form_id = (issuer.request_forms or {}).get(type_id) if issuer else None
    if (
        issuer is None
        or issuer.published_at is None
        or issuer.identity.did is None
        or type_id not in (issuer.credential_types or [])
        or form_id is None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="offer_not_found")
    form = await db.get(Form, uuid.UUID(form_id))
    if form is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="offer_not_found")
    return issuer, form


async def offer_view(db: AsyncSession, issuer: Issuer, form: Form, type_id: str) -> dict[str, Any]:
    """What a holder sees of an offer: the issuer, the credential type and the
    form — its fields with their definitions, the credentials it asks for,
    each with its type's name (the tenant's own types are not Almena's)."""
    filled = form_credentials.fills(form.credentials, form.fields)
    known = await trust_anchor.load(db, issuer.tenant_id)

    def asked(entry: dict[str, Any]) -> dict[str, Any]:
        kind = known.types.get(entry["type"])
        return {**entry, "fills": filled[entry["key"]], "labels": kind.labels if kind else {}}

    return {
        "issuer": {
            "slug": issuer.slug,
            "name": issuer.name,
            "description": issuer.description,
            "did": issuer.identity.did,
        },
        "credential_type": credentials.type_out(known.types[type_id]),
        "form": {
            "slug": form.slug,
            "name": form.name,
            "description": form.description,
            "fields": [
                {**stored, "key": key, "field": catalog.field_out(item)}
                for key, stored, item in await checks.fields_of(db, form)
            ],
            "credentials": [asked(entry) for entry in form.credentials],
        },
    }


@router.get(
    "/issuers/{issuer_slug}/offers/{type_id}",
    summary="Public: an issuer's offer of a credential type, with its form",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`offer_not_found`"}},
)
async def get_offer(issuer_slug: str, type_id: str, db: DbSession) -> dict[str, Any]:
    issuer, form = await offering(db, issuer_slug, type_id)
    return await offer_view(db, issuer, form, type_id)


class OfferIssuer(BaseModel):
    slug: str
    name: str
    # By language.
    description: dict[str, str] | None
    did: str


class OfferCategory(BaseModel):
    id: str
    labels: dict[str, str]


class OfferType(BaseModel):
    id: str
    labels: dict[str, str]
    descriptions: dict[str, str]
    category: OfferCategory


class OfferEntry(BaseModel):
    issuer: OfferIssuer
    credential_type: OfferType


class OfferPage(BaseModel):
    items: list[OfferEntry]
    # Pass it back as `cursor` for the next page; `null` on the last one.
    next_cursor: str | None
    # Every offer listed, on every page.
    total: int


def _offered(
    known: trust_anchor.Catalogue, granted: list[str] | None, forms: dict[str, str] | None
) -> list[str]:
    """The types an issuer offers, in the order it grants them: granted, with a
    form to apply, and still in the catalogue."""
    return [t for t in granted or [] if t in (forms or {}) and t in known.types]


def _offers_of(known: trust_anchor.Catalogue, issuer: Issuer) -> list[str]:
    return _offered(known, issuer.credential_types, issuer.request_forms)


class _Catalogues:
    """The catalogue as each tenant sees it, loaded once: issuers offer their
    tenant's own types beside Almena's."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.seen: dict[uuid.UUID, trust_anchor.Catalogue] = {}

    async def of(self, tenant_id: uuid.UUID) -> trust_anchor.Catalogue:
        if tenant_id not in self.seen:
            self.seen[tenant_id] = await trust_anchor.load(self.db, tenant_id)
        return self.seen[tenant_id]


def _offer_cursor(issuer: Issuer, type_id: str) -> str:
    assert issuer.published_at is not None
    return paging.encode(issuer.published_at, issuer.id, type_id)


def _offer_position(cursor: str) -> tuple[datetime, uuid.UUID, str]:
    moment, issuer_id, type_id = paging.decode(cursor, 3)
    try:
        return datetime.fromisoformat(moment), uuid.UUID(issuer_id), type_id
    except ValueError:
        raise paging.invalid() from None


def _offer_entry(known: trust_anchor.Catalogue, issuer: Issuer, type_id: str) -> OfferEntry:
    item = known.types[type_id]
    return OfferEntry(
        issuer=OfferIssuer(
            slug=issuer.slug,
            name=issuer.name,
            description=issuer.description,
            did=issuer.identity.did or "",
        ),
        credential_type=OfferType(
            id=item.id,
            labels=item.labels,
            descriptions=item.descriptions,
            category=OfferCategory(
                id=item.category, labels=known.type_categories.get(item.category, {})
            ),
        ),
    )


@router.get(
    "/offers",
    summary="Public: every published issuer's offers, newest issuer first",
    responses={status.HTTP_400_BAD_REQUEST: {"description": "`invalid_cursor`"}},
)
async def list_offers(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
) -> OfferPage:
    # Only issuers whose DID resolves; each offer is one of their types.
    listed = (Issuer.published_at.is_not(None), Identity.did.is_not(None))
    query = (
        select(Issuer)
        .join(Issuer.identity)
        .where(*listed)
        .order_by(Issuer.published_at.desc(), Issuer.id.desc())
    )
    granted = await db.execute(
        select(Issuer.tenant_id, Issuer.credential_types, Issuer.request_forms)
        .join(Issuer.identity)
        .where(*listed)
    )
    catalogues = _Catalogues(db)
    total = 0
    for tenant_id, types, forms in granted:
        total += len(_offered(await catalogues.of(tenant_id), types, forms))
    after: str | None = None
    if cursor:
        moment, issuer_id, after = _offer_position(cursor)
        # The issuer the last page ended in comes again, from after its type.
        query = query.where(
            or_(
                Issuer.published_at < moment,
                and_(Issuer.published_at == moment, Issuer.id <= issuer_id),
            )
        )
        first: uuid.UUID | None = issuer_id
    else:
        first = None
    found: list[tuple[Issuer, str]] = []
    batch, skip = max(limit, 20), 0
    while len(found) <= limit:
        issuers = list(await db.scalars(query.offset(skip).limit(batch)))
        for issuer in issuers:
            types = _offers_of(await catalogues.of(issuer.tenant_id), issuer)
            if issuer.id == first and after is not None:
                types = types[types.index(after) + 1 :] if after in types else []
            found.extend((issuer, type_id) for type_id in types)
        if len(issuers) < batch:
            break
        skip += batch
    page = found[:limit]
    more = len(found) > limit
    return OfferPage(
        items=[
            _offer_entry(await catalogues.of(issuer.tenant_id), issuer, type_id)
            for issuer, type_id in page
        ],
        next_cursor=_offer_cursor(*page[-1]) if more else None,
        total=total,
    )
