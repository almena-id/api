"""Add credential issuance to applications: the draft claims, the credential.

Revision ID: 8a4d2f6c1e39
Revises: 6f1c3b8e2a97
Create Date: 2026-10-03 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8a4d2f6c1e39"
down_revision: str | Sequence[str] | None = "6f1c3b8e2a97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("issuance_claims", sa.JSON()),
    ("credential_valid_until", sa.DateTime(timezone=True)),
    ("credential", sa.Text()),
    ("issued_at", sa.DateTime(timezone=True)),
    ("delivered_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    """Upgrade schema."""
    for name, kind in COLUMNS:
        op.add_column("applications", sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for name, _ in reversed(COLUMNS):
        op.drop_column("applications", name)
