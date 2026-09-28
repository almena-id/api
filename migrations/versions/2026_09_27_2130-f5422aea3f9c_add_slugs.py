"""Add slugs to tenants, identities, issuers and verifiers.

Revision ID: f5422aea3f9c
Revises: d92145f54db3
Create Date: 2026-09-27 21:30:00.000000

"""

import secrets
import string
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5422aea3f9c"
down_revision: str | Sequence[str] | None = "d92145f54db3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Table and slug prefix. The generator is copied from models/base.py rather
# than imported, so this migration keeps working if that code changes.
TABLES = {"tenants": "ten", "identities": "idn", "issuers": "iss", "verifiers": "ver"}
ALPHABET = string.ascii_lowercase + string.digits


def _slug(prefix: str) -> str:
    return prefix + "_" + "".join(secrets.choice(ALPHABET) for _ in range(12))


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    for table, prefix in TABLES.items():
        op.add_column(table, sa.Column("slug", sa.String(length=32), nullable=True))
        # Existing rows get one now, as new rows do when created.
        ids: Sequence[object] = bind.execute(sa.text(f"SELECT id FROM {table}")).scalars().all()
        for row_id in ids:
            bind.execute(
                sa.text(f"UPDATE {table} SET slug = :slug WHERE id = :id"),
                {"slug": _slug(prefix), "id": row_id},
            )
        op.alter_column(table, "slug", nullable=False)
        op.create_unique_constraint(op.f(f"uq_{table}_slug"), table, ["slug"])


def downgrade() -> None:
    """Downgrade schema."""
    for table in reversed(TABLES):
        op.drop_constraint(op.f(f"uq_{table}_slug"), table, type_="unique")
        op.drop_column(table, "slug")
