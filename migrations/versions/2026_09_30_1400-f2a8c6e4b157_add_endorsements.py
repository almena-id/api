"""Add endorsements: an issuer's, verifier's or mediator's whois.vp, its
tenant's signed endorsement, and until when it holds.

Revision ID: f2a8c6e4b157
Revises: e1f7a3c5d820
Create Date: 2026-09-30 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a8c6e4b157"
down_revision: str | Sequence[str] | None = "e1f7a3c5d820"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema. What was published without an endorsement goes back to
    a draft: publishing is endorsing now (nothing is in production)."""
    op.add_column("identities", sa.Column("presentation", sa.Text(), nullable=True))
    op.add_column(
        "identities", sa.Column("endorsed_until", sa.DateTime(timezone=True), nullable=True)
    )
    for table in ("issuers", "verifiers", "mediators"):
        op.execute(f"UPDATE {table} SET published_at = NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("identities", "endorsed_until")
    op.drop_column("identities", "presentation")
