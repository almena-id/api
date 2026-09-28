"""Name unnamed tenants after their admin, as new ones are.

Revision ID: cf7cd7fa8f67
Revises: ab0c7e1db9a6
Create Date: 2026-09-26 23:26:47.996409

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cf7cd7fa8f67"
down_revision: str | Sequence[str] | None = "ab0c7e1db9a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # The language each account signed up in is not known: English, the default.
    op.execute(
        """
        UPDATE tenants SET name = LEFT('Tenant of ' || (
            SELECT u.email FROM tenant_members m JOIN users u ON u.id = m.user_id
            WHERE m.tenant_id = tenants.id AND m.role = 'admin'
            ORDER BY m.created_at LIMIT 1
        ), 200)
        WHERE name IS NULL AND EXISTS (
            SELECT 1 FROM tenant_members m WHERE m.tenant_id = tenants.id AND m.role = 'admin'
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Names given since cannot be told from these; they are left as they are.
