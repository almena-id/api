"""Catalogues: the fields forms ask for and the credential types issuers grant.

Every tenant keeps fields of its own here, for its own forms. The trust
anchor's entries — the root tenant's (`registry_api.trust_anchor`) — are
everyone's: its fields, value domains, categories and credential types make
Almena's catalogue, published on the identity domain and used by every tenant.
Nothing of it is written in code: it is data, seeded when the anchor is made.
"""

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin, slug_column


class CatalogField(TimestampMixin, Base):
    """A field: its key (the claim name too, for the anchor's), its type, its
    labels per language, where it comes from (`source`, a standard), its
    category and what its type needs — a length and a pattern for text, the
    options of a list or the value domain it draws on, the formats of a file,
    a group's parts. The anchor's are referred to by their key, a tenant's own
    as `custom:{key}` (see `registry_api.api.routes.custom_fields`)."""

    __tablename__ = "catalog_fields"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("fld")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(16))
    labels: Mapped[dict[str, str]] = mapped_column(JSON)
    # The standard it is named after; empty for a tenant's own.
    source: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # One of the anchor's field categories; empty for a tenant's own.
    category: Mapped[str] = mapped_column(String(32), default="", server_default="")
    # `max_length`, `pattern`, `options`, `formats`, `domain`, `values`,
    # `repeatable` or `parts`, as its type takes them.
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)


class CatalogDomain(TimestampMixin, Base):
    """A value domain of the anchor's: the codes a coded or file field takes
    (ISO 3166-1 countries, ISO 639-1 languages, file formats…), each with its
    labels per language and, for file formats, its media type."""

    __tablename__ = "catalog_domains"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(64))
    labels: Mapped[dict[str, str]] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(200))
    # `[{"value", "labels", "media_type"?}]`, in the order they are shown.
    codes: Mapped[list[dict[str, Any]]] = mapped_column(JSON)


class CatalogCategory(TimestampMixin, Base):
    """A category of the anchor's catalogue, for its fields (`kind: field`) or
    its credential types (`kind: credential`), with its labels."""

    __tablename__ = "catalog_categories"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    key: Mapped[str] = mapped_column(String(32))
    labels: Mapped[dict[str, str]] = mapped_column(JSON)


class CatalogType(TimestampMixin, Base):
    """A credential type of the anchor's: its claims, each one of the anchor's
    fields, and how each format names it (`vct`, W3C type, mdoc doctype).
    `issuance: almena` types are granted by tenants' issuers; `external` ones
    (the EU PID) are issued under a framework of their own, only asked for."""

    __tablename__ = "catalog_types"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("cty")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(64))
    labels: Mapped[dict[str, str]] = mapped_column(JSON)
    descriptions: Mapped[dict[str, str]] = mapped_column(JSON)
    category: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(200))
    # `[{"field", "required"}]`, in the order they are shown.
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    issuance: Mapped[str] = mapped_column(String(16), default="almena", server_default="almena")
    # Set for a type named elsewhere (`urn:eudi:pid:1`); the anchor's own
    # `vct` is derived from its key.
    vct: Mapped[str | None] = mapped_column(Text)
    w3c_type: Mapped[str | None] = mapped_column(String(100))
    mdoc_doctype: Mapped[str | None] = mapped_column(String(200))
