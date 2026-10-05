"""Subscriptions: what a tenant pays for, and so what it may do.

Revision ID: d5f3a8c1e604
Revises: b4e1c7a9d258
Create Date: 2026-10-12 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5f3a8c1e604"
down_revision: str | Sequence[str] | None = "b4e1c7a9d258"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    now = sa.text("now()")
    op.create_table(
        "subscriptions",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_subscriptions_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_subscriptions_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_subscriptions")),
    )


def downgrade() -> None:
    op.drop_table("subscriptions")
