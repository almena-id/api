"""Add the tenant's signer: the member who signs under `single_user`.

Revision ID: e4b7d2c9a615
Revises: c7e2a9d4f318
Create Date: 2026-10-01 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b7d2c9a615"
down_revision: str | Sequence[str] | None = "c7e2a9d4f318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("tenants", sa.Column("signer_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_tenants_signer_id"), "tenants", ["signer_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_tenants_signer_id_users"),
        "tenants",
        "users",
        ["signer_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("fk_tenants_signer_id_users"), "tenants", type_="foreignkey")
    op.drop_index(op.f("ix_tenants_signer_id"), table_name="tenants")
    op.drop_column("tenants", "signer_id")
