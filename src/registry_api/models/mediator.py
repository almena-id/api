"""A mediator: the mailbox the tenant's identities receive DIDComm through.

The registry does not run it: it is registered here with the address it
listens on, and the registry gives it its identity (DID), whose document names
that address. The tenant and each of its issuers and verifiers pick one of the
tenant's mediators, and their documents route messages through its DID.

A public mediator is offered to every tenant, not only its own: once
published, any tenant may pick it for itself and its issuers and verifiers.
The root's (Almena's) is public, and new tenants start with it."""

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from registry_api.models.base import Base, PublishedMixin, TimestampMixin, slug_column
from registry_api.models.identity import Identity


class Mediator(PublishedMixin, TimestampMixin, Base):
    __tablename__ = "mediators"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("med")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    # Where it listens: https (plain http only on loopback).
    url: Mapped[str] = mapped_column(String(2048))
    # Offered to every tenant (once published), not only its own.
    public: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Its DID: an identity of its own, created with it and named like it.
    identity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identities.id", ondelete="RESTRICT"), index=True, unique=True
    )

    identity: Mapped[Identity] = relationship(lazy="joined")
