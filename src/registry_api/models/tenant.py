"""Tenants: the organisation a user works in. Everything in the portal belongs to one."""

import uuid
from typing import Literal

from sqlalchemy import JSON, Boolean, ForeignKey, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base, TimestampMixin, slug_column

# `admin` also manages who belongs; `member` works with everything else.
Role = Literal["admin", "member"]
ROLES: tuple[Role, ...] = ("admin", "member")


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"
    # At most one root.
    __table_args__ = (
        Index(
            "uq_tenants_root",
            "root",
            unique=True,
            postgresql_where=text("root"),
            sqlite_where=text("root"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = slug_column("ten")
    # The tenant an account starts with is named after its email ("Tenant of …");
    # older ones were named so by a migration, in English.
    name: Mapped[str | None] = mapped_column(String(200))
    # The organisation's own identity (DID), one of its identities, created with
    # it and named like it. Nullable only because the two rows point at each
    # other: a tenant is always given one as it is created.
    identity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("identities.id", ondelete="SET NULL", use_alter=True),
        index=True,
    )
    # The root authority and trust anchor (Almena Trust Anchor), whose catalogue
    # every tenant uses (`registry_api.trust_anchor`); created once at install (`registry-api
    # init-root`). Its identity is the identity domain's own DID.
    root: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # The mediator the tenant's own identity receives messages through: one of
    # its own or a public one (a new tenant starts with the root's). Its
    # issuers and verifiers each pick their own.
    mediator_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("mediators.id", ondelete="SET NULL", use_alter=True),
        index=True,
    )
    # Who signs as the tenant: one of `signing_flows.FLOWS`.
    signing_flow: Mapped[str] = mapped_column(
        String(32), default="any_admin", server_default="any_admin"
    )
    # `single_user`: the member who signs. Kept if they leave, signing nothing,
    # until somebody else is chosen.
    signer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # The languages it works in, of the platform's (`field_catalog.LANGUAGES`),
    # in the platform's order: one at least. A new tenant starts with the one
    # its creator signed up in; the trust anchor has them all, always — its
    # catalogue is everyone's, named in every one.
    languages: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["en"])


class TenantMember(TimestampMixin, Base):
    """Who belongs to which tenant."""

    __tablename__ = "tenant_members"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="member", server_default="member")


class TenantInvitation(TimestampMixin, Base):
    """Somebody asked into a tenant by email. They join, with the role, the
    next time they sign in with that address; until then they are pending."""

    __tablename__ = "tenant_invitations"
    __table_args__ = (UniqueConstraint("tenant_id", "email"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Lowercased, like users.email, so the match on sign-in is exact.
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(16))
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
