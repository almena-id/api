"""The offers: a published issuer's credential types that have a request form
(`issuer_credentials`), listed in the public catalogue (``GET /catalog/offers``,
searched by text and filtered by category, whose categories with offers are
``GET /catalog/offers/categories``), an issuer's together
(``GET /catalog/issuers/{slug}/offers``) and opened one by one
(``GET /catalog/issuers/{slug}/offers/{type}``); holders apply for them through
the catalog (`routes.applications`)."""

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
from sqlalchemy import Select, select
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
from registry_api.texts import text_of

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
    """What a holder sees of an offer: the issuer, the credential type (and its
    category) and the form — its fields with their definitions, the credentials it asks for,
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
        # The type's category, named and with what it is drawn with.
        "category": _type_out(known, type_id).category.model_dump(),
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
    # What it is drawn with (`field_catalog.CATEGORY_ICONS`).
    icon: str


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
    # Every offer listed that matches, on every page.
    total: int


class OfferCategoryCount(OfferCategory):
    # How many offers are filed under it.
    offers: int


class IssuerOffers(BaseModel):
    issuer: OfferIssuer
    published_at: datetime
    # Its offers, in the order it grants them.
    offers: list[OfferType]


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


def _issuer_out(issuer: Issuer) -> OfferIssuer:
    return OfferIssuer(
        slug=issuer.slug,
        name=issuer.name,
        description=issuer.description,
        did=issuer.identity.did or "",
    )


def _type_out(known: trust_anchor.Catalogue, type_id: str) -> OfferType:
    item = known.types[type_id]
    return OfferType(
        id=item.id,
        labels=item.labels,
        descriptions=item.descriptions,
        category=OfferCategory(
            id=item.category,
            labels=known.type_categories.get(item.category, {}),
            icon=known.type_icon(item.category),
        ),
    )


def _offer_entry(known: trust_anchor.Catalogue, issuer: Issuer, type_id: str) -> OfferEntry:
    return OfferEntry(issuer=_issuer_out(issuer), credential_type=_type_out(known, type_id))


def _matches(entry: OfferEntry, words: list[str]) -> bool:
    """Whether every word is in the offer's name, description, category or
    issuer, in any language, whatever the case."""
    kind = entry.credential_type
    said = " ".join(
        [
            entry.issuer.name,
            *kind.labels.values(),
            *kind.descriptions.values(),
            *kind.category.labels.values(),
            *(entry.issuer.description or {}).values(),
        ]
    ).casefold()
    return all(word in said for word in words)


def _listed() -> Select[Issuer]:
    """Published issuers whose DID resolves, newest first."""
    return (
        select(Issuer)
        .join(Issuer.identity)
        .where(Issuer.published_at.is_not(None), Identity.did.is_not(None))
        .order_by(Issuer.published_at.desc(), Issuer.id.desc())
    )


async def _every_offer(db: AsyncSession) -> list[tuple[Issuer, int, OfferEntry]]:
    """Every offer in the catalogue, newest issuer first, each issuer's in the
    order it grants them (its place in that order beside it)."""
    catalogues = _Catalogues(db)
    found: list[tuple[Issuer, int, OfferEntry]] = []
    for issuer in await db.scalars(_listed()):
        known = await catalogues.of(issuer.tenant_id)
        found.extend(
            (issuer, place, _offer_entry(known, issuer, type_id))
            for place, type_id in enumerate(_offers_of(known, issuer))
        )
    return found


@router.get(
    "/offers",
    summary="Public: every published issuer's offers, newest issuer first",
    responses={status.HTTP_400_BAD_REQUEST: {"description": "`invalid_cursor`"}},
)
async def list_offers(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
    # Words each offer must have, in its names, descriptions, category or issuer.
    q: Annotated[str | None, Query(max_length=100)] = None,
    # A category of credential types (`/catalog/offers/categories`).
    category: Annotated[str | None, Query(max_length=32)] = None,
) -> OfferPage:
    every = await _every_offer(db)
    words = (q or "").casefold().split()
    matching = [
        (issuer, place, entry)
        for issuer, place, entry in every
        if (not category or entry.credential_type.category.id == category)
        and _matches(entry, words)
    ]
    found = matching
    if cursor:
        moment, issuer_id, after = _offer_position(cursor)
        # Where the last page ended in its issuer's offers; past them all when
        # that type is no longer offered.
        ended = next(
            (
                place
                for issuer, place, entry in every
                if issuer.id == issuer_id and entry.credential_type.id == after
            ),
            len(every),
        )

        def beyond(issuer: Issuer, place: int) -> bool:
            assert issuer.published_at is not None
            if issuer.published_at != moment:
                return issuer.published_at < moment
            if issuer.id != issuer_id:
                return issuer.id < issuer_id
            return place > ended

        found = [offer for offer in matching if beyond(offer[0], offer[1])]
    page = found[:limit]
    return OfferPage(
        items=[entry for _, _, entry in page],
        next_cursor=(
            _offer_cursor(page[-1][0], page[-1][2].credential_type.id)
            if len(found) > limit
            else None
        ),
        total=len(matching),
    )


@router.get(
    "/offers/categories",
    summary="Public: the categories offers are filed under, with how many each has",
)
async def list_offer_categories(db: DbSession) -> list[OfferCategoryCount]:
    counted: dict[str, OfferCategoryCount] = {}
    for _, _, entry in await _every_offer(db):
        kind = entry.credential_type.category
        if kind.id in counted:
            counted[kind.id].offers += 1
        else:
            counted[kind.id] = OfferCategoryCount(**kind.model_dump(), offers=1)
    # The most offered first; ties by name in English.
    return sorted(counted.values(), key=lambda c: (-c.offers, text_of(c.labels, "en") or c.id))


@router.get(
    "/issuers/{issuer_slug}/offers",
    summary="Public: a published issuer and everything it offers",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`issuer_not_found`"}},
)
async def get_issuer_offers(issuer_slug: str, db: DbSession) -> IssuerOffers:
    issuer = await db.scalar(_listed().where(Issuer.slug == issuer_slug))
    if issuer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="issuer_not_found")
    assert issuer.published_at is not None
    known = await trust_anchor.load(db, issuer.tenant_id)
    return IssuerOffers(
        issuer=_issuer_out(issuer),
        published_at=issuer.published_at,
        offers=[_type_out(known, type_id) for type_id in _offers_of(known, issuer)],
    )
