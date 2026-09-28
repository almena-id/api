"""Declarative base shared by every model."""

import secrets
import string
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import DateTime, MetaData, String, func
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
