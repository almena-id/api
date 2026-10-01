"""Forms refer to Almena's field catalogue: drop forms with fields of their own.

Forms made before the catalogue defined their fields themselves (a key and a
type each). They cannot be turned into catalogue references reliably, and none
was used by a flow yet, so they are removed.

Revision ID: 7b1d4e8a2c56
Revises: 5e9b2d7c4a81
Create Date: 2026-10-02 09:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b1d4e8a2c56"
down_revision: str | Sequence[str] | None = "5e9b2d7c4a81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "DELETE FROM forms WHERE EXISTS ("
        "  SELECT 1 FROM json_array_elements(fields) AS f WHERE NOT (f::jsonb ? 'ref')"
        ")"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Catalogue references mean nothing to the earlier shape.
    op.execute("DELETE FROM forms")
