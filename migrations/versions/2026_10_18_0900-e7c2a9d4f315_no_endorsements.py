"""Drop endorsements: publishing is an admin's decision, not a signature, so
an issuer's, verifier's or mediator's whois.vp and until when it held go.

Revision ID: e7c2a9d4f315
Revises: d9b3f6a2c814
Create Date: 2026-10-18 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7c2a9d4f315"
down_revision: str | Sequence[str] | None = "d9b3f6a2c814"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("identities", "endorsed_until")
    op.drop_column("identities", "presentation")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("identities", sa.Column("presentation", sa.Text(), nullable=True))
    op.add_column(
        "identities", sa.Column("endorsed_until", sa.DateTime(timezone=True), nullable=True)
    )
