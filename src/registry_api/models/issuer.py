"""An issuer of credentials, run from the tenant's members' wallets."""

import uuid

from sqlalchemy import JSON, ForeignKey, String, Uuid
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


class Issuer(QueueMixin, SigningMixin, PublishedMixin, TimestampMixin, Base):
    __tablename__ = "issuers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("iss")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    # By language (`registry_api.texts`); none until written.
    description: Mapped[dict[str, str] | None] = mapped_column(JSON)
    # The DID it issues as: an identity of its own, created with it and named
    # like it (never shared with the tenant or another issuer or verifier).
    identity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identities.id", ondelete="RESTRICT"), index=True, unique=True
    )

    # The mediator it receives messages through (the tenant's, or a public
    # one); none until chosen.
    mediator_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("mediators.id", ondelete="SET NULL"), index=True
    )

    # The credential types it grants, by id in Almena's catalogue
    # (`registry_api.credential_catalog`); none until declared.
    credential_types: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    # The form a holder fills in to ask for each of them, by type id: the
    # issuer's offer (`{type_id: form_id}`); a type without one is not offered.
    request_forms: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, server_default="{}")

    identity: Mapped[Identity] = relationship(lazy="joined")
    mediator: Mapped[Mediator | None] = relationship(lazy="joined")
