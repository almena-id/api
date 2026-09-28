"""A tenant's certification by Almena: a request, and what became of it.

A tenant asks with its legal name, a domain it proves it controls (a DNS TXT
record) and its logo; an Almena reviewer approves or rejects it. The approved
one is the tenant's certification until another is approved, which supersedes
it; changing anything opens a new request and leaves the approved one in force.
"""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin

# `draft` is being filled in, `in_review` waits for a reviewer, `approved` is
# in force, `rejected` says why not (editing it makes it a draft again), and
# `superseded` was in force until a later one was approved.
Status = Literal["draft", "in_review", "approved", "rejected", "superseded"]
# The ones a tenant is still working on: at most one per tenant.
OPEN: tuple[Status, ...] = ("draft", "in_review", "rejected")


class Certification(TimestampMixin, Base):
    __tablename__ = "certifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    # Lowercased, without scheme or path; IDNs in their ASCII form.
    domain: Mapped[str | None] = mapped_column(String(253))
    # What the TXT record at `_almena.{domain}` must say, and when it did.
    dns_token: Mapped[str | None] = mapped_column(String(64))
    domain_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # PNG, JPEG or WebP, at most 256 KB; loaded only when asked for.
    logo: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    logo_type: Mapped[str | None] = mapped_column(String(32))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    # Why it was rejected, for the tenant to read.
    reason: Mapped[str | None] = mapped_column(Text)
