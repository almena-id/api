"""A holder's application to an issuer for a credential, and its files."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin, slug_column

# `open` (waiting for the holder's wallet), `paired` (the holder's DID bound;
# filling in), `submitted` (signed by that DID), then the issuer's decision,
# and `issued` once the issuer's signer has signed the credential.
APPLICATION_STATUSES = ("open", "paired", "submitted", "accepted", "rejected", "issued")


class Application(TimestampMixin, Base):
    """What a holder fills in, presents and signs to ask an issuer for a
    credential (see `registry_api.api.routes.applications`). Holders have no
    account: whoever started it holds its secret, and its wallet answers its
    requests; the issuer's tenant reads it once submitted."""

    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("app")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    issuer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("issuers.id", ondelete="CASCADE"), index=True
    )
    form_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("forms.id", ondelete="CASCADE"))
    credential_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # SHA-256 of the secret whoever started it keeps.
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    # The holder's DID for this issuer: the `did:key` its wallet paired with.
    holder_did: Mapped[str | None] = mapped_column(String(255))
    # The wallet request in course — `pair`, `present` or `submit` — its nonce,
    # until when it holds and whether the wallet answered it.
    wallet_purpose: Mapped[str | None] = mapped_column(String(16))
    wallet_nonce: Mapped[str | None] = mapped_column(String(64))
    wallet_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wallet_answered: Mapped[bool] = mapped_column(default=False)
    # The answers typed, checked against the form; the credentials presented,
    # as verified (`registry_api.presentations`).
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    presented: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # What the holder signed: its content's SHA-256 (JCS) and the JWS.
    digest: Mapped[str | None] = mapped_column(String(64))
    signature: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Issuing: the claims the issuer settled on and until when it holds (the
    # draft its signer signs), then the credential as signed — an SD-JWT VC
    # with all its disclosures — and when the holder's wallet took it.
    issuance_claims: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    credential_valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApplicationFile(TimestampMixin, Base):
    """A file a holder uploaded for one of the form's file fields."""

    __tablename__ = "application_files"
    __table_args__ = (UniqueConstraint("application_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    # The form's field it answers.
    key: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer)
    # `sha256-{base64}`, as the catalogue's file schema writes it.
    digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)
