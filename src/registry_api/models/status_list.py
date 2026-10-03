"""An issuer's status list: where its credentials say whether they still hold."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin, slug_column


class StatusList(TimestampMixin, Base):
    """An IETF Token Status List (see `registry_api.status_lists`): one status
    per credential the issuer issued with an index in it, public at its URI
    once its issuer's signer has signed it. Nobody signs on the server: every
    change is a new list the signer's wallet signs, and only then kept."""

    __tablename__ = "status_lists"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Its URI's last part: `{public_url}/status-lists/{slug}`.
    slug: Mapped[str] = slug_column("stl")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    issuer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("issuers.id", ondelete="CASCADE"), index=True
    )
    # How many statuses it holds, of `status_lists.BITS` bits each.
    size: Mapped[int] = mapped_column(Integer)
    # The statuses as last signed, packed as the list carries them (before
    # compression); each signature makes a new revision.
    statuses: Mapped[bytes] = mapped_column(LargeBinary)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    # The Status List Token as signed (`statuslist+jwt`); none until the first.
    token: Mapped[str | None] = mapped_column(Text)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
