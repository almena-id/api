"""A tenant's subscription: what it pays for, and so what it may do.

One per tenant at most; none is the free use of the platform. The trust
anchor grants and ends them for now (`registry_api.api.routes.accounts`);
payments will drive them later. What each plan lets a tenant do is
`registry_api.entitlements`'.
"""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin

# `active`: paid; `past_due`: a payment missed, still in use during the grace
# days; `canceled`: ended, kept for the record.
SubscriptionStatus = Literal["active", "past_due", "canceled"]
STATUSES: tuple[SubscriptionStatus, ...] = ("active", "past_due", "canceled")


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    # One of `entitlements.PLANS`.
    plan: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    # Until when it is paid; `null`, open-ended (granted by hand).
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why, for whoever manages it (an invoice, an agreement).
    note: Mapped[str | None] = mapped_column(Text)
    # Who last set it.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
