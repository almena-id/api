"""A verifier of credentials, run from the tenant's members' wallets."""

import uuid

from sqlalchemy import ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from registry_api.models.base import (
    Base,
    PublishedMixin,
    QueueMixin,
    SigningMixin,
    TimestampMixin,
    slug_column,
)
from registry_api.models.identity import Identity
from registry_api.models.mediator import Mediator


class Verifier(QueueMixin, SigningMixin, PublishedMixin, TimestampMixin, Base):
    __tablename__ = "verifiers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("ver")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # The DID it asks for presentations as: an identity of its own, created
    # with it and named like it (never shared with the tenant or another
    # issuer or verifier).
    identity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identities.id", ondelete="RESTRICT"), index=True, unique=True
    )

    # The mediator it receives messages through (the tenant's, or a public
    # one); none until chosen.
    mediator_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("mediators.id", ondelete="SET NULL"), index=True
    )

    identity: Mapped[Identity] = relationship(lazy="joined")
    mediator: Mapped[Mediator | None] = relationship(lazy="joined")
