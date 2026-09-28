"""Add certifications: a tenant's requests to Almena, and the one in force.

Revision ID: 5c1f8e3a92d4
Revises: 7e2d4a9c1b30
Create Date: 2026-09-28 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5c1f8e3a92d4"
down_revision: str | Sequence[str] | None = "7e2d4a9c1b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "certifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("legal_name", sa.String(length=200), nullable=True),
        sa.Column("domain", sa.String(length=253), nullable=True),
        sa.Column("dns_token", sa.String(length=64), nullable=True),
        sa.Column("domain_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("logo", sa.LargeBinary(), nullable=True),
        sa.Column("logo_type", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_certifications_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name=op.f("fk_certifications_reviewed_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_certifications_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_certifications")),
    )
    op.create_index(op.f("ix_certifications_status"), "certifications", ["status"], unique=False)
    op.create_index(
        op.f("ix_certifications_tenant_id"), "certifications", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_certifications_tenant_id"), table_name="certifications")
    op.drop_index(op.f("ix_certifications_status"), table_name="certifications")
    op.drop_table("certifications")
