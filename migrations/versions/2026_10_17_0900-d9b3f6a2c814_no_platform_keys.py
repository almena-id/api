"""Drop the keys whose private halves lived in the vault: the platform keeps none.

The tenant's platform key (flow `none`) and issuers' and verifiers' messaging
keys go; a tenant under `none` moves to `any_admin`.

Revision ID: d9b3f6a2c814
Revises: c4e8a1f7d263
Create Date: 2026-10-17 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9b3f6a2c814"
down_revision: str | Sequence[str] | None = "c4e8a1f7d263"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "UPDATE tenants SET signing_flow = 'any_admin', signer_id = NULL "
        "WHERE signing_flow = 'none'"
    )
    op.drop_column("tenants", "platform_key")
    op.drop_column("identities", "agreement_key")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("identities", sa.Column("agreement_key", sa.String(length=64), nullable=True))
    op.add_column("tenants", sa.Column("platform_key", sa.String(length=64), nullable=True))
