"""An issuer of credentials, run from the tenant's members' wallets."""

import uuid

from sqlalchemy import ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from registry_api.models.base import Base, TimestampMixin, slug_column
from registry_api.models.identity import Identity


class Issuer(TimestampMixin, Base):
    __tablename__ = "issuers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("iss")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # The DID it issues as: one of the tenant's identities, which it may share
    # with other issuers and verifiers of the same tenant.
    identity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identities.id", ondelete="RESTRICT"), index=True
    )

    identity: Mapped[Identity] = relationship(lazy="joined")
