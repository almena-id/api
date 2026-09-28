"""Add the root tenant: Almena, the authority the others are certified by.

Revision ID: 9a4c2e7f1d05
Revises: 5c1f8e3a92d4
Create Date: 2026-09-28 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a4c2e7f1d05"
down_revision: str | Sequence[str] | None = "5c1f8e3a92d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tenants", sa.Column("root", sa.Boolean(), server_default="false", nullable=False)
    )
    op.create_index(
        "uq_tenants_root",
        "tenants",
        ["root"],
        unique=True,
        postgresql_where=sa.text("root"),
        sqlite_where=sa.text("root"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_tenants_root", table_name="tenants")
    op.drop_column("tenants", "root")
