"""Social sign-in: flows in progress, and the provider accounts linked to users."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin


class OAuthFlow(TimestampMixin, Base):
    """A sign-in sent to a provider and not yet back: lives a few minutes."""

    __tablename__ = "oauth_flows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # SHA-256 of the `state` parameter; the state itself travels with the browser.
    state_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    provider: Mapped[str] = mapped_column(String(32))
    # PKCE verifier and OIDC nonce: needed again when the provider answers.
    code_verifier: Mapped[str] = mapped_column(String(128))
    nonce: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserIdentity(TimestampMixin, Base):
    """A provider account (Google, GitHub…) that signs a user in."""

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    # The provider's stable id for the account; the email may change, this does not.
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(320))
