"""Add forms' credentials: the credentials a form asks to be presented.

Revision ID: 2e7a9c4d6b18
Revises: 9d2b7e4f1a63
Create Date: 2026-10-02 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2e7a9c4d6b18"
down_revision: str | Sequence[str] | None = "9d2b7e4f1a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "forms",
        sa.Column("credentials", sa.JSON(), server_default="[]", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("forms", "credentials")
