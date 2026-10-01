"""Add public mediators: offered to every tenant, the root's first.

Revision ID: 8c5f3a1d7e29
Revises: e4b7d2c9a615
Create Date: 2026-10-01 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c5f3a1d7e29"
down_revision: str | Sequence[str] | None = "e4b7d2c9a615"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "mediators",
        sa.Column("public", sa.Boolean(), server_default="false", nullable=False),
    )
    # The mediator the root was created with is public, as new roots' are.
    op.execute(
        "UPDATE mediators SET public = true "
        "WHERE name = 'Almena Mediator' "
        "AND tenant_id IN (SELECT id FROM tenants WHERE root)"
    )
    # Tenants with no mediator get the one new tenants start with, if published.
    op.execute(
        "UPDATE tenants SET mediator_id = ("
        "  SELECT m.id FROM mediators m JOIN tenants r ON r.id = m.tenant_id"
        "  WHERE r.root AND m.public AND m.published_at IS NOT NULL"
        "  ORDER BY m.created_at, m.id LIMIT 1"
        ") WHERE mediator_id IS NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Tenants on another tenant's mediator cannot keep it.
    op.execute(
        "UPDATE tenants SET mediator_id = NULL WHERE mediator_id IN "
        "(SELECT m.id FROM mediators m WHERE m.tenant_id <> tenants.id)"
    )
    for table in ("issuers", "verifiers"):
        op.execute(
            f"UPDATE {table} SET mediator_id = NULL WHERE mediator_id IN "
            f"(SELECT m.id FROM mediators m WHERE m.tenant_id <> {table}.tenant_id)"
        )
    op.drop_column("mediators", "public")
