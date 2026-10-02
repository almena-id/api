"""API tokens are sessions with a name (no idle end), and wallet requests say
who asked: the portal or the CLI (the wallet's audience).

Revision ID: 3d8b1f5a7c20
Revises: 6c2f8a1d4e93
Create Date: 2026-10-06 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3d8b1f5a7c20"
down_revision: str | Sequence[str] | None = "6c2f8a1d4e93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("name", sa.String(length=100), nullable=True))
    op.add_column(
        "wallet_requests",
        sa.Column("client", sa.String(length=16), nullable=False, server_default="portal"),
    )


def downgrade() -> None:
    op.drop_column("wallet_requests", "client")
    op.drop_column("sessions", "name")
