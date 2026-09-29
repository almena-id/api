"""Social sign-in: flows in progress, the provider accounts linked to users, and
moves out of an empty account."""

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
    # Set when a signed-in account is linking the provider rather than signing in.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
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
    # What the provider showed when it was linked, to tell accounts apart; a
    # provider linked from the account screen need not vouch for one.
    email: Mapped[str | None] = mapped_column(String(320))


class AccountMove(TimestampMixin, Base):
    """Leave an empty account for the one a way in already belongs to.

    Linking a way in (an email, a provider account) that another account owns
    is refused; when the account doing it is still empty, the answer carries a
    ticket that moves its ways in to the owner and deletes it. Proving the way
    in, just now, is what shows the caller controls the owner too.
    """

    __tablename__ = "account_moves"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # SHA-256 of the ticket; the ticket itself goes back to the portal once.
    ticket_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    from_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    to_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
