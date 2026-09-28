"""Add publication: issuers, verifiers and mediators are drafts until published.

Revision ID: 4d7b1a9e3c62
Revises: 9a4c2e7f1d05
Create Date: 2026-09-28 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d7b1a9e3c62"
down_revision: str | Sequence[str] | None = "9a4c2e7f1d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("issuers", "verifiers", "mediators")


def upgrade() -> None:
    """Upgrade schema."""
    for table in TABLES:
        op.add_column(table, sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index(f"ix_{table}_published_at", table, ["published_at"])


def downgrade() -> None:
    """Downgrade schema."""
    for table in TABLES:
        op.drop_index(f"ix_{table}_published_at", table_name=table)
        op.drop_column(table, "published_at")
