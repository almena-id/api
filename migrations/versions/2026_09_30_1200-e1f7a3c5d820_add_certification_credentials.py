"""Add certification credentials: the signed credential of an approved
certification, until when it holds, and the presentation that publishes it.

Revision ID: e1f7a3c5d820
Revises: c4d8e2a6b913
Create Date: 2026-09-30 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f7a3c5d820"
down_revision: str | Sequence[str] | None = "c4d8e2a6b913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("certifications", sa.Column("credential", sa.Text(), nullable=True))
    op.add_column(
        "certifications", sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("certifications", sa.Column("presentation", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("certifications", "presentation")
    op.drop_column("certifications", "valid_until")
    op.drop_column("certifications", "credential")
