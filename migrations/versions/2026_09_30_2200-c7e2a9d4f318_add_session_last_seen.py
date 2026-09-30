"""Add when each session was last used: sessions end after idle time too.

Revision ID: c7e2a9d4f318
Revises: a3f9c1e7b542
Create Date: 2026-09-30 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e2a9d4f318"
down_revision: str | Sequence[str] | None = "a3f9c1e7b542"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Open sessions count as seen now: they get the idle time from here.
    op.add_column(
        "sessions",
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("sessions", "last_seen_at")
