"""Category icons: what each category of the catalogue is drawn with, one of
`field_catalog.CATEGORY_ICONS`. The anchor's seeded categories take the icon
`catalogue-v1.json` gives them; any other starts as `tag`.

Revision ID: a4c8e1f2b937
Revises: e7c2a9d4f315
Create Date: 2026-11-01 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c8e1f2b937"
down_revision: str | Sequence[str] | None = "e7c2a9d4f315"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEEDED = {
    ("field", "person"): "user",
    ("field", "contact"): "mail",
    ("field", "document"): "file-text",
    ("field", "education"): "graduation-cap",
    ("field", "membership"): "users",
    ("field", "file"): "folder",
    ("credential", "identity"): "id-card",
    ("credential", "contact"): "mail",
    ("credential", "education"): "graduation-cap",
    ("credential", "work"): "briefcase",
    ("credential", "membership"): "users",
}


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "catalog_categories",
        sa.Column("icon", sa.String(length=32), server_default="tag", nullable=False),
    )
    categories = sa.table(
        "catalog_categories", sa.column("kind"), sa.column("key"), sa.column("icon")
    )
    for (kind, key), icon in SEEDED.items():
        op.execute(
            categories.update()
            .where(categories.c.kind == kind, categories.c.key == key)
            .values(icon=icon)
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("catalog_categories", "icon")
