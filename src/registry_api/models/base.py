"""Declarative base shared by every model."""

import secrets
import string
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, MetaData, String, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so Alembic migrations are stable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PublishedMixin:
    """Issuers, verifiers and mediators start as drafts, seen only inside their
    tenant: their DID does not resolve and no catalogue lists them. An admin
    publishes one (`published_at` set) and may take it back (`null` again)."""

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class SigningMixin:
    """How an issuer or a verifier signs: its signing system, and whatever that
    system needs. Nobody signs on the server: people do, from their wallets.

    The catalogue so far has one system, `single_user`: one member of the
    tenant, named by `signer_id`, signs alone. `null` is not configured yet.
    """

    signing: Mapped[str | None] = mapped_column(String(32))
    signer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )


class QueueMixin:
    """An issuer's or a verifier's queue at the broker (`registry_api.broker`):
    `subject.{slug}`, read by a user named like it. When it was made; `null`
    while it has none."""

    queue_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    if TYPE_CHECKING:
        slug: str

    @property
    def queue_name(self) -> str:
        return f"subject.{self.slug}"


# Slugs: a type prefix and 12 random lowercase letters and digits (~62 bits),
# e.g. `iss_k3v9x0q2mzt8`. Generated once, never derived from the name, so a
# rename does not move them; unique per table, whatever the tenant.
SLUG_ALPHABET = string.ascii_lowercase + string.digits
SLUG_RANDOM_LENGTH = 12


def new_slug(prefix: str) -> str:
    return prefix + "_" + "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_RANDOM_LENGTH))


def slug_factory(prefix: str) -> Callable[[], str]:
    return lambda: new_slug(prefix)


def slug_column(prefix: str) -> Mapped[str]:
    return mapped_column(String(32), unique=True, default=slug_factory(prefix))
