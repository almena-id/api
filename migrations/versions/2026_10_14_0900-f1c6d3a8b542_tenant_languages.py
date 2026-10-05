"""Tenant languages: the platform's languages each tenant works in.

Revision ID: f1c6d3a8b542
Revises: e8a4b2d6c917
Create Date: 2026-10-14 09:00:00.000000

Tenants made before keep every language of the platform: what they wrote may
be in any of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1c6d3a8b542"
down_revision: str | Sequence[str] | None = "e8a4b2d6c917"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The platform's languages when this was written (`field_catalog.LANGUAGES`).
LANGUAGES = ["en", "es"]


def upgrade() -> None:
    op.add_column("tenants", sa.Column("languages", sa.JSON(), nullable=True))
    tenants = sa.table("tenants", sa.column("languages", sa.JSON()))
    # The column's JSON type encodes the list itself.
    op.execute(tenants.update().values(languages=LANGUAGES))
    with op.batch_alter_table("tenants") as batch:
        batch.alter_column("languages", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("languages")
