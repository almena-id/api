"""Add the tenant's platform key: the platform signs as it under `none`.

Revision ID: c4e8a1f7d263
Revises: a7c2e9f4b318
Create Date: 2026-10-16 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8a1f7d263"
down_revision: str | Sequence[str] | None = "a7c2e9f4b318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # The public half, as a multikey; the private one is in the vault. Made
    # the first time a tenant chooses `none`.
    op.add_column("tenants", sa.Column("platform_key", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tenants", "platform_key")
