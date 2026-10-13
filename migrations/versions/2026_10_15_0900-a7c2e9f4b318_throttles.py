"""Throttles: counters of what callers do, per key and window.

Revision ID: a7c2e9f4b318
Revises: f1c6d3a8b542
Create Date: 2026-10-15 09:00:00.000000

Sign-in codes per address, wrong codes per address, and the public requests
that cost something per network address (registry_api.throttle).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c2e9f4b318"
down_revision: str | Sequence[str] | None = "f1c6d3a8b542"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "throttles",
        sa.Column("key", sa.String(length=400), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_throttles")),
    )
    op.create_index(op.f("ix_throttles_window_start"), "throttles", ["window_start"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_throttles_window_start"), table_name="throttles")
    op.drop_table("throttles")
