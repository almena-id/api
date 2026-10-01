"""Add issuers' credential types: what each grants, from Almena's catalogue.

Revision ID: 9d2b7e4f1a63
Revises: 3c8f6a2e9d14
Create Date: 2026-10-02 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d2b7e4f1a63"
down_revision: str | Sequence[str] | None = "3c8f6a2e9d14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "issuers",
        sa.Column("credential_types", sa.JSON(), server_default="[]", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("issuers", "credential_types")
