"""Add signing: how an issuer or a verifier signs (one member, for now).

Revision ID: 8e3f5c2a7b14
Revises: 4d7b1a9e3c62
Create Date: 2026-09-28 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8e3f5c2a7b14"
down_revision: str | Sequence[str] | None = "4d7b1a9e3c62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("issuers", "verifiers")


def upgrade() -> None:
    """Upgrade schema."""
    for table in TABLES:
        op.add_column(table, sa.Column("signing", sa.String(length=32), nullable=True))
        op.add_column(table, sa.Column("signer_id", sa.Uuid(), nullable=True))
        op.create_index(f"ix_{table}_signer_id", table, ["signer_id"])
        op.create_foreign_key(
            f"fk_{table}_signer_id_users",
            table,
            "users",
            ["signer_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    for table in TABLES:
        op.drop_constraint(f"fk_{table}_signer_id_users", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_signer_id", table_name=table)
        op.drop_column(table, "signer_id")
        op.drop_column(table, "signing")
