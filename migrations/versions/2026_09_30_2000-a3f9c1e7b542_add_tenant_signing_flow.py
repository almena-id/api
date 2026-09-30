"""Add the tenant's signing flow: who signs as the tenant.

Revision ID: a3f9c1e7b542
Revises: d5e1b8f3a046
Create Date: 2026-09-30 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f9c1e7b542"
down_revision: str | Sequence[str] | None = "d5e1b8f3a046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Every tenant so far signed by any one admin: that is the flow they keep.
    op.add_column(
        "tenants",
        sa.Column(
            "signing_flow",
            sa.String(length=32),
            server_default="any_admin",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tenants", "signing_flow")
