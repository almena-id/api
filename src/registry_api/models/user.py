"""Portal accounts: who signs in to the registry portal."""

import uuid

from sqlalchemy import String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Stored lowercased, so uniqueness does not depend on how it was typed.
    # There is no password: owning the mailbox is what signs somebody in.
    email: Mapped[str] = mapped_column(String(320), unique=True)
    # What the person likes to be called; the email stands in while there is none.
    alias: Mapped[str | None] = mapped_column(String(100))
