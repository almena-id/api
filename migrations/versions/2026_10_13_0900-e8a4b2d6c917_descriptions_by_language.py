"""Issuers' and verifiers' descriptions by language, as forms' are.

A description kept as one text becomes `{"en": text}`: English, the language
every reader falls back to (`registry_api.texts.text_of`). Their names stay one
text — the entity's own, carried in its DIDs and credentials.

Revision ID: e8a4b2d6c917
Revises: d5f3a8c1e604
Create Date: 2026-10-13 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8a4b2d6c917"
down_revision: str | Sequence[str] | None = "d5f3a8c1e604"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("issuers", "verifiers")


def upgrade() -> None:
    for table in TABLES:
        op.alter_column(
            table,
            "description",
            type_=sa.JSON(),
            postgresql_using=(
                "CASE WHEN description IS NULL THEN NULL "
                "ELSE json_build_object('en', description) END"
            ),
        )


def downgrade() -> None:
    for table in TABLES:
        # The English text, else the Spanish one (the portal's languages).
        op.alter_column(
            table,
            "description",
            type_=sa.Text(),
            postgresql_using="COALESCE(description->>'en', description->>'es')",
        )
