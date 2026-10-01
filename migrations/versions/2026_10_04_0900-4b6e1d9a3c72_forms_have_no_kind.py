"""Forms have no kind: the same form serves an issuer's offer or a verifier's.

Revision ID: 4b6e1d9a3c72
Revises: 8a4d2f6c1e39
Create Date: 2026-10-04 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b6e1d9a3c72"
down_revision: str | Sequence[str] | None = "8a4d2f6c1e39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("forms", "kind")


def downgrade() -> None:
    """Downgrade schema."""
    # Every form there was asked for credentials.
    op.add_column(
        "forms",
        sa.Column(
            "kind", sa.String(length=32), server_default="credential_request", nullable=False
        ),
    )
