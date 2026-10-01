"""Add custom fields: the fields a tenant adds to the catalogue for its forms.

Revision ID: 3c8f6a2e9d14
Revises: 7b1d4e8a2c56
Create Date: 2026-10-02 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c8f6a2e9d14"
down_revision: str | Sequence[str] | None = "7b1d4e8a2c56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "custom_fields",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
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
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_custom_fields_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_custom_fields")),
        sa.UniqueConstraint("slug", name=op.f("uq_custom_fields_slug")),
        sa.UniqueConstraint("tenant_id", "key", name=op.f("uq_custom_fields_tenant_id")),
    )
    op.create_index(
        op.f("ix_custom_fields_tenant_id"), "custom_fields", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_custom_fields_tenant_id"), table_name="custom_fields")
    op.drop_table("custom_fields")
