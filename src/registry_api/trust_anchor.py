"""The trust anchor: the root tenant, whose catalogue is everyone's.

Tenants keep fields and — when their subscription allows it
(`registry_api.entitlements`) — credential types of their own beside it, as
`custom:{key}`, for their own forms and issuers.

Almena's catalogue — the fields forms ask for, the value domains they draw on,
their categories and the credential types issuers grant — is not written in
code: it is the anchor's data (`registry_api.models.catalog`), kept like any
tenant keeps its own fields. What the anchor keeps, every tenant uses: its
fields are referred to by their key and published on the identity domain, its
credential types are what issuers grant and forms ask for. A tenant's own
fields stay its own, as `custom:{key}`.

The anchor is the root (`registry_api.root`): its identity is the identity
domain's DID, under which the catalogue is published. Its catalogue is seeded
when it is made, from ``assets/catalogue-v1.json``; from then on it grows as
its members add to it.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import credential_catalog as credentials
from registry_api import field_catalog as catalog
from registry_api.models import (
    CatalogCategory,
    CatalogDomain,
    CatalogField,
    CatalogType,
    Tenant,
)

ANCHOR_NAME = "Almena Trust Anchor"
# How a tenant's own fields and types are referred to; the anchor's go by their key.
PREFIX = credentials.PREFIX
SEED = Path(__file__).parent / "assets" / "catalogue-v1.json"


@dataclass(frozen=True)
class Catalogue:
    """The catalogue as one tenant sees it: the anchor's fields (by key) and,
    for a tenant other than the anchor, its own (`custom:{key}`); the anchor's
    domains, categories and credential types."""

    fields: dict[str, catalog.Field]
    domains: dict[str, catalog.Domain]
    field_categories: dict[str, catalog.Labels]
    types: dict[str, credentials.CredentialType]
    type_categories: dict[str, catalog.Labels]

    def anchor_fields(self) -> list[catalog.Field]:
        """The anchor's fields, in the catalogue's order."""
        return [item for item in self.fields.values() if item.published]

    def fields_out(self) -> dict[str, Any]:
        return catalog.catalogue_out(self.anchor_fields(), self.domains, self.field_categories)

    def types_out(self) -> dict[str, Any]:
        return credentials.catalogue_out(list(self.types.values()), self.type_categories)


async def anchor_id(db: AsyncSession) -> uuid.UUID | None:
    """The anchor: the root tenant; none before it is made."""
    return await db.scalar(select(Tenant.id).where(Tenant.root))


def as_field(item: CatalogField, domains: dict[str, catalog.Domain], anchor: bool) -> catalog.Field:
    """A kept field as forms use it: the anchor's under its key, published; a
    tenant's own as `custom:{key}`, kept to itself."""
    if anchor:
        return catalog.field_from(
            item.key, item.type, item.labels, item.definition, domains, item.source, item.category
        )
    return catalog.field_from(
        PREFIX + item.key,
        item.type,
        item.labels,
        item.definition,
        domains,
        "custom",
        "custom",
        published=False,
    )


def type_data(item: CatalogType) -> dict[str, Any]:
    return {
        "key": item.key,
        "labels": item.labels,
        "descriptions": item.descriptions,
        "category": item.category,
        "source": item.source,
        "claims": item.claims,
        "issuance": item.issuance,
        "vct": item.vct,
        "w3c_type": item.w3c_type,
        "mdoc_doctype": item.mdoc_doctype,
    }


async def load(db: AsyncSession, tenant_id: uuid.UUID | None = None) -> Catalogue:
    """The catalogue as `tenant_id` sees it: the anchor's, and — when it is not
    the anchor — its own fields and credential types (`custom:{key}`)."""
    anchor = await anchor_id(db)
    domains: dict[str, catalog.Domain] = {}
    field_categories: dict[str, catalog.Labels] = {}
    type_categories: dict[str, catalog.Labels] = {}
    found: dict[str, catalog.Field] = {}
    types: dict[str, credentials.CredentialType] = {}
    if anchor is not None:
        for domain in await db.scalars(
            select(CatalogDomain)
            .where(CatalogDomain.tenant_id == anchor)
            .order_by(CatalogDomain.created_at, CatalogDomain.key)
        ):
            domains[domain.key] = catalog.Domain(
                domain.key,
                domain.labels,
                domain.source,
                tuple(catalog.code_of(code) for code in domain.codes),
            )
        for category in await db.scalars(
            select(CatalogCategory)
            .where(CatalogCategory.tenant_id == anchor)
            .order_by(CatalogCategory.created_at, CatalogCategory.key)
        ):
            kept = field_categories if category.kind == "field" else type_categories
            kept[category.key] = category.labels
        for item in await db.scalars(
            select(CatalogField)
            .where(CatalogField.tenant_id == anchor)
            .order_by(CatalogField.created_at, CatalogField.key)
        ):
            found[item.key] = as_field(item, domains, anchor=True)
        for kind in await db.scalars(
            select(CatalogType)
            .where(CatalogType.tenant_id == anchor)
            .order_by(CatalogType.created_at, CatalogType.key)
        ):
            types[kind.key] = credentials.type_from(type_data(kind))
    if tenant_id is not None and tenant_id != anchor:
        for item in await db.scalars(
            select(CatalogField)
            .where(CatalogField.tenant_id == tenant_id)
            .order_by(CatalogField.created_at, CatalogField.key)
        ):
            found[PREFIX + item.key] = as_field(item, domains, anchor=False)
        slug = await db.scalar(select(Tenant.slug).where(Tenant.id == tenant_id))
        for kind in await db.scalars(
            select(CatalogType)
            .where(CatalogType.tenant_id == tenant_id)
            .order_by(CatalogType.created_at, CatalogType.key)
        ):
            types[PREFIX + kind.key] = credentials.type_from(type_data(kind), slug)
    return Catalogue(found, domains, field_categories, types, type_categories)


@cache
def seed_data() -> dict[str, Any]:
    """The catalogue the anchor starts with: Almena's, version 1."""
    data: dict[str, Any] = json.loads(SEED.read_text(encoding="utf-8"))
    return data


async def seed(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Give the anchor the catalogue it starts with, leaving alone whatever it
    has already. Entries keep the seed's order (`created_at`)."""
    data = seed_data()
    start = datetime.now(UTC)
    tick = iter(start + timedelta(microseconds=n) for n in range(10_000))
    have = {
        (kind, key)
        for kind, key in await db.execute(
            select(CatalogCategory.kind, CatalogCategory.key).where(
                CatalogCategory.tenant_id == tenant_id
            )
        )
    }
    for category in data["categories"]:
        if (category["kind"], category["key"]) not in have:
            db.add(CatalogCategory(tenant_id=tenant_id, created_at=next(tick), **category))
    have_domains = set(
        await db.scalars(select(CatalogDomain.key).where(CatalogDomain.tenant_id == tenant_id))
    )
    for domain in data["domains"]:
        if domain["key"] not in have_domains:
            db.add(CatalogDomain(tenant_id=tenant_id, created_at=next(tick), **domain))
    have_fields = set(
        await db.scalars(select(CatalogField.key).where(CatalogField.tenant_id == tenant_id))
    )
    for field in data["fields"]:
        if field["key"] not in have_fields:
            db.add(CatalogField(tenant_id=tenant_id, created_at=next(tick), **field))
    have_types = set(
        await db.scalars(select(CatalogType.key).where(CatalogType.tenant_id == tenant_id))
    )
    for kind in data["credential_types"]:
        if kind["key"] not in have_types:
            db.add(CatalogType(tenant_id=tenant_id, created_at=next(tick), **kind))
    await db.flush()
