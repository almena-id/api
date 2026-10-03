"""A verifier asking a wallet to present credentials: one QR, one answer."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin


class Verification(TimestampMixin, Base):
    """What a verifier of the tenant asked for (a form's credentials), the
    request's nonce and how long it holds, and — once the wallet answered —
    the verdict, as `POST …/forms/{id}/verify` would give it."""

    __tablename__ = "verifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    verifier_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("verifiers.id", ondelete="CASCADE"), index=True
    )
    form_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("forms.id", ondelete="CASCADE"))
    # Who showed the code.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    nonce: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The verdict: every required credential presented and verified.
    verified: Mapped[bool | None] = mapped_column(Boolean)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
