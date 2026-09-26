"""One-time sign-in codes sent by email: at most one live code per address."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin


class LoginCode(TimestampMixin, Base):
    __tablename__ = "login_codes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Not a foreign key: the first code is sent before the account exists.
    email: Mapped[str] = mapped_column(String(320), unique=True)
    # SHA-256 of the code; the code itself only ever travels in the email.
    code_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
