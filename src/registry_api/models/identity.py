"""An identity (DID) the tenant holds: the tenant's register of DIDs. The
tenant and each of its issuers, verifiers and mediators have one of their own.

Its DID is a `did:webvh` this API serves for its slug (see `registry_api.dids`),
and exists once a tenant admin has signed its first log entry from a wallet;
until then it is pending. The log itself is `DidLogEntry`."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
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
    # `did:webvh:{SCID}:…`, set when the first log entry is signed.
    did: Mapped[str | None] = mapped_column(String(255), unique=True)
    # An issuer's, verifier's or mediator's `whois.vp`: its tenant's signed
    # endorsement, presented by the tenant as its controller; set when it is
    # published, cleared when it is taken back. And until when it holds.
    presentation: Mapped[str | None] = mapped_column(Text)
    endorsed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # An issuer's or verifier's messaging key (X25519, a multikey): the public
    # half, listed under `keyAgreement`; the private one is in the vault (see
    # `registry_api.messaging_keys`). Set when its signer first signs it.
    agreement_key: Mapped[str | None] = mapped_column(String(64))


class DidLogEntry(TimestampMixin, Base):
    """One line of an identity's did:webvh log, as published: signed."""

    __tablename__ = "did_log_entries"
    __table_args__ = (UniqueConstraint("identity_id", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    identity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("identities.id", ondelete="CASCADE"), index=True
    )
    # The number in its `versionId` (1, 2, …).
    version: Mapped[int] = mapped_column(Integer)
    # The entry as a JSON line, proof included, exactly as served.
    entry: Mapped[str] = mapped_column(Text)
