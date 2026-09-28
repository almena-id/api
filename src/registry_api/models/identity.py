"""An identity (DID) the tenant holds: the tenant's register of DIDs. The
tenant and each of its issuers and verifiers have one of their own.

Its DID is the `did:web` this API serves for its slug (see `registry_api.dids`);
where its keys live is still to be decided, and arrives as columns then."""

import uuid

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin, slug_column


class Identity(TimestampMixin, Base):
    __tablename__ = "identities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("idn")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
