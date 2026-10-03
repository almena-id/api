"""Where a holder receives messages from the issuer it applied to.

Revision ID: 5a9c3e7d1f24
Revises: 8f4a2c6e1b37
Create Date: 2026-10-08 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5a9c3e7d1f24"
down_revision: str | Sequence[str] | None = "8f4a2c6e1b37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "applications", sa.Column("holder_messaging_did", sa.String(length=2048), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("applications", "holder_messaging_did")
