"""Requests to an Almena wallet: sign in, or link the wallet to an account."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin


class WalletRequest(TimestampMixin, Base):
    """What the portal shows as a QR code or deep link, until a wallet answers.

    Its id travels in the QR code, so it is public; only the portal that asked
    holds the poll secret that turns the answer into a session.
    """

    __tablename__ = "wallet_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # SHA-256 of the secret the portal polls with.
    poll_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    nonce: Mapped[str] = mapped_column(String(64))
    # `sign_in`, `link` for the account in `user_id`, or `sign`: that account
    # signs `payload` (a did:webvh log entry) with its wallet.
    purpose: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    # The portal's language: names the tenant a new account starts with.
    locale: Mapped[str] = mapped_column(String(8))
    # `sign`: what is to be signed — `{"identity_id", "entry", "signers"}` as JSON.
    payload: Mapped[str | None] = mapped_column(Text)
    # The wallet's DID once it has answered.
    did: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
