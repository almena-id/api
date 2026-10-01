"""The fields a tenant adds to Almena's catalogue for its own forms."""

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin, slug_column


class CustomField(TimestampMixin, Base):
    """A field of the tenant's own: its key (never one of Almena's ids), its
    type, its labels per language and what its type needs — a length and a
    pattern for text, the options of a list, the formats of a file. Forms
    refer to it as `custom:{key}` (see `registry_api.api.routes.custom_fields`)."""

    __tablename__ = "custom_fields"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("fld")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(16))
    labels: Mapped[dict[str, str]] = mapped_column(JSON)
    # `max_length`, `pattern`, `options` or `formats`, as its type takes them.
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
