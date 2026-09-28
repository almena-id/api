"""Give each issuer and verifier an identity of its own.

Revision ID: 3b8e5d0c71a2
Revises: f5422aea3f9c
Create Date: 2026-09-28 10:00:00.000000

"""

import secrets
import string
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b8e5d0c71a2"
down_revision: str | Sequence[str] | None = "f5422aea3f9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("issuers", "verifiers")
# Copied from models/base.py rather than imported, as in the slugs migration.
ALPHABET = string.ascii_lowercase + string.digits


def _slug() -> str:
    return "idn_" + "".join(secrets.choice(ALPHABET) for _ in range(12))


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    # Identities were shareable until now: the tenant's own stays the tenant's,
    # the first to use any other keeps it, and the rest get a new one named like them.
    taken = set(
        bind.execute(sa.text("SELECT identity_id FROM tenants WHERE identity_id IS NOT NULL"))
        .scalars()
        .all()
    )
    rows = bind.execute(
        sa.text(
            "SELECT 'issuers' AS t, id, tenant_id, name, identity_id, created_at FROM issuers"
            " UNION ALL"
            " SELECT 'verifiers', id, tenant_id, name, identity_id, created_at FROM verifiers"
            " ORDER BY created_at"
        )
    ).all()
    for table, row_id, tenant_id, name, identity_id, created_at in rows:
        if identity_id not in taken:
            taken.add(identity_id)
            continue
        new_id = uuid.uuid4()
        bind.execute(
            sa.text(
                "INSERT INTO identities (id, slug, tenant_id, name, created_at, updated_at)"
                " VALUES (:id, :slug, :tenant_id, :name, :created_at, now())"
            ),
            {
                "id": new_id,
                "slug": _slug(),
                "tenant_id": tenant_id,
                "name": name,
                "created_at": created_at,
            },
        )
        bind.execute(
            sa.text(f"UPDATE {table} SET identity_id = :identity_id WHERE id = :id"),
            {"identity_id": new_id, "id": row_id},
        )
    for table in TABLES:
        op.drop_index(op.f(f"ix_{table}_identity_id"), table_name=table)
        op.create_index(op.f(f"ix_{table}_identity_id"), table, ["identity_id"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    for table in TABLES:
        op.drop_index(op.f(f"ix_{table}_identity_id"), table_name=table)
        op.create_index(op.f(f"ix_{table}_identity_id"), table, ["identity_id"], unique=False)
