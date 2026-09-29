"""The domains a tenant links to itself: each proved by a DNS TXT record."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin


class TenantDomain(TimestampMixin, Base):
    """A domain the tenant says is its own; verified once the TXT record at
    `_almena.{domain}` says `almena-verify={dns_token}`. Verified ones are
    named in the tenant's DID document (`LinkedDomains`)."""

    __tablename__ = "tenant_domains"
    __table_args__ = (UniqueConstraint("tenant_id", "domain"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Lowercased, without scheme or path; IDNs in their ASCII form.
    domain: Mapped[str] = mapped_column(String(253))
    dns_token: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
