"""Sign-in sessions: an opaque bearer token per signed-in browser or CLI.

One ends at `expires_at`, set at sign-in, or earlier when no request has used
it for the idle time (`last_seen_at`); both are settings.

An API token is a session too, with a `name`: made by its account for scripts
and CI (``/auth/me/tokens``), it lasts the days it was made for and never ends
for being idle.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from registry_api.models.base import Base, TimestampMixin
from registry_api.models.user import User


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # SHA-256 of the token: a leaked table does not hand out sessions.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # An API token's name; `null` for a sign-in.
    name: Mapped[str | None] = mapped_column(String(100))

    user: Mapped[User] = relationship(lazy="joined")
