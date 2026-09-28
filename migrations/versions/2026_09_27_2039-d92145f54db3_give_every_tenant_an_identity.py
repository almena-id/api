"""Give every tenant an identity of its own, named like it.

Revision ID: d92145f54db3
Revises: a931213af61b
Create Date: 2026-09-27 20:39:34.316592

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d92145f54db3"
down_revision: str | Sequence[str] | None = "a931213af61b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("tenants", sa.Column("identity_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_tenants_identity_id"), "tenants", ["identity_id"], unique=False)
    # Each existing tenant gets its identity, as a new one does when created.
    op.execute("UPDATE tenants SET identity_id = gen_random_uuid()")
    op.execute(
        """
        INSERT INTO identities (id, tenant_id, name, created_at, updated_at)
        SELECT identity_id, id, COALESCE(name, 'Tenant'), created_at, now() FROM tenants
        """
    )
    op.create_foreign_key(
        op.f("fk_tenants_identity_id_identities"),
        "tenants",
        "identities",
        ["identity_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("fk_tenants_identity_id_identities"), "tenants", type_="foreignkey")
    op.drop_index(op.f("ix_tenants_identity_id"), table_name="tenants")
    op.drop_column("tenants", "identity_id")
