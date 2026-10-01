"""Add applications: holders asking issuers for credentials; issuers' offers.

Revision ID: 6f1c3b8e2a97
Revises: 2e7a9c4d6b18
Create Date: 2026-10-03 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6f1c3b8e2a97"
down_revision: str | Sequence[str] | None = "2e7a9c4d6b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "issuers", sa.Column("request_forms", sa.JSON(), server_default="{}", nullable=False)
    )
    op.create_table(
        "applications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("issuer_id", sa.Uuid(), nullable=False),
        sa.Column("form_id", sa.Uuid(), nullable=False),
        sa.Column("credential_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("secret_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("holder_did", sa.String(length=255), nullable=True),
        sa.Column("wallet_purpose", sa.String(length=16), nullable=True),
        sa.Column("wallet_nonce", sa.String(length=64), nullable=True),
        sa.Column("wallet_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wallet_answered", sa.Boolean(), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("presented", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=True),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_applications_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issuer_id"],
            ["issuers.id"],
            name=op.f("fk_applications_issuer_id_issuers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["form_id"],
            ["forms.id"],
            name=op.f("fk_applications_form_id_forms"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applications")),
        sa.UniqueConstraint("slug", name=op.f("uq_applications_slug")),
        sa.UniqueConstraint("secret_hash", name=op.f("uq_applications_secret_hash")),
    )
    for column in ("tenant_id", "issuer_id", "status"):
        op.create_index(op.f(f"ix_applications_{column}"), "applications", [column], unique=False)
    op.create_table(
        "application_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_application_files_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_files")),
        sa.UniqueConstraint(
            "application_id", "key", name=op.f("uq_application_files_application_id")
        ),
    )
    op.create_index(
        op.f("ix_application_files_application_id"),
        "application_files",
        ["application_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_application_files_application_id"), table_name="application_files")
    op.drop_table("application_files")
    for column in ("tenant_id", "issuer_id", "status"):
        op.drop_index(op.f(f"ix_applications_{column}"), table_name="applications")
    op.drop_table("applications")
    op.drop_column("issuers", "request_forms")
