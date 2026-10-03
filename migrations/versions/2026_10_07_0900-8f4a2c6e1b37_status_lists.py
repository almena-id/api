"""Issuers' status lists, and each issued credential's entry and status in them.

Revision ID: 8f4a2c6e1b37
Revises: 3d8b1f5a7c20
Create Date: 2026-10-07 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f4a2c6e1b37"
down_revision: str | Sequence[str] | None = "3d8b1f5a7c20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "status_lists",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("issuer_id", sa.Uuid(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("statuses", sa.LargeBinary(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("token", sa.Text(), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["issuer_id"],
            ["issuers.id"],
            name=op.f("fk_status_lists_issuer_id_issuers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_status_lists_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_status_lists")),
        sa.UniqueConstraint("slug", name=op.f("uq_status_lists_slug")),
    )
    op.create_index(op.f("ix_status_lists_tenant_id"), "status_lists", ["tenant_id"])
    op.create_index(op.f("ix_status_lists_issuer_id"), "status_lists", ["issuer_id"])
    op.add_column("applications", sa.Column("status_list_id", sa.Uuid(), nullable=True))
    op.add_column("applications", sa.Column("status_index", sa.Integer(), nullable=True))
    op.add_column(
        "applications", sa.Column("credential_status", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "applications",
        sa.Column("credential_status_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_applications_status_list_id_status_lists"),
        "applications",
        "status_lists",
        ["status_list_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_applications_status_list_id"), "applications", ["status_list_id"])
    op.create_unique_constraint(
        op.f("uq_applications_status_list_id"),
        "applications",
        ["status_list_id", "status_index"],
    )
    # Credentials issued before status lists hold, and cannot be revoked.
    op.execute("UPDATE applications SET credential_status = 'valid' WHERE status = 'issued'")


def downgrade() -> None:
    op.drop_constraint(op.f("uq_applications_status_list_id"), "applications", type_="unique")
    op.drop_index(op.f("ix_applications_status_list_id"), table_name="applications")
    op.drop_constraint(
        op.f("fk_applications_status_list_id_status_lists"), "applications", type_="foreignkey"
    )
    op.drop_column("applications", "credential_status_at")
    op.drop_column("applications", "credential_status")
    op.drop_column("applications", "status_index")
    op.drop_column("applications", "status_list_id")
    op.drop_index(op.f("ix_status_lists_issuer_id"), table_name="status_lists")
    op.drop_index(op.f("ix_status_lists_tenant_id"), table_name="status_lists")
    op.drop_table("status_lists")
