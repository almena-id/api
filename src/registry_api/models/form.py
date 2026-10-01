"""The forms a tenant puts to people in its flows."""

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin, slug_column


class Form(TimestampMixin, Base):
    """A form: its fields in order — each a field of the catalogue, by id,
    with what the form adds to it — and the credentials it asks to be
    presented (see `registry_api.api.routes.forms`). It is the same form
    whoever puts it: an issuer's offer, or a verifier's."""

    __tablename__ = "forms"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("frm")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # By language (`registry_api.texts`), as its fields' help is.
    name: Mapped[dict[str, str]] = mapped_column(JSON)
    description: Mapped[dict[str, str] | None] = mapped_column(JSON)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    # The credentials it asks to be presented (`registry_api.form_credentials`).
    credentials: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, server_default="[]"
    )
