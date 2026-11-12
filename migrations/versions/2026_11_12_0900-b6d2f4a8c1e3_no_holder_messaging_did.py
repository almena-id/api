"""No holder messaging DID: the `did:peer:2` a wallet named at pairing is no
longer asked for nor kept. Nothing was ever sent to it — the platform keeps no
issuer messaging keys — and the wallet no longer names one.

Revision ID: b6d2f4a8c1e3
Revises: a4c8e1f2b937
Create Date: 2026-11-12 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d2f4a8c1e3"
down_revision: str | Sequence[str] | None = "a4c8e1f2b937"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("applications", "holder_messaging_did")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "applications", sa.Column("holder_messaging_did", sa.String(length=2048), nullable=True)
    )
