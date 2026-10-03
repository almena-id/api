"""When each issuer's and verifier's queue at the broker was made.

Revision ID: 7e2b4d9c1a35
Revises: 5a9c3e7d1f24
Create Date: 2026-10-09 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7e2b4d9c1a35"
down_revision: str | Sequence[str] | None = "5a9c3e7d1f24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("issuers", "verifiers"):
        op.add_column(
            table, sa.Column("queue_created_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    for table in ("issuers", "verifiers"):
        op.drop_column(table, "queue_created_at")
