"""Add mediators, each with an identity of its own.

The tenant's single mediator (an origin and the did:web it served) becomes a
mediator of the tenant, with an identity the registry gives it; the tenant and
all its issuers and verifiers, which used it until now, are pointed at it.

Revision ID: 7e2d4a9c1b30
Revises: 3b8e5d0c71a2
Create Date: 2026-09-28 12:00:00.000000

"""

import secrets
import string
import uuid
from collections.abc import Sequence
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7e2d4a9c1b30"
down_revision: str | Sequence[str] | None = "3b8e5d0c71a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("tenants", "issuers", "verifiers")
# Copied from models/base.py rather than imported, as in the slugs migration.
ALPHABET = string.ascii_lowercase + string.digits


def _slug(prefix: str) -> str:
    return f"{prefix}_" + "".join(secrets.choice(ALPHABET) for _ in range(12))


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "mediators",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["identities.id"],
            name=op.f("fk_mediators_identity_id_identities"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_mediators_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mediators")),
        sa.UniqueConstraint("slug", name=op.f("uq_mediators_slug")),
    )
    op.create_index(op.f("ix_mediators_identity_id"), "mediators", ["identity_id"], unique=True)
    op.create_index(op.f("ix_mediators_tenant_id"), "mediators", ["tenant_id"], unique=False)
    for table in TABLES:
        op.add_column(table, sa.Column("mediator_id", sa.Uuid(), nullable=True))
        op.create_index(op.f(f"ix_{table}_mediator_id"), table, ["mediator_id"], unique=False)
        op.create_foreign_key(
            op.f(f"fk_{table}_mediator_id_mediators"),
            table,
            "mediators",
            ["mediator_id"],
            ["id"],
            ondelete="SET NULL",
        )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, mediator_url FROM tenants WHERE mediator_url IS NOT NULL")
    ).all()
    for tenant_id, url in rows:
        name = urlsplit(url).hostname or url
        identity_id, mediator_id = uuid.uuid4(), uuid.uuid4()
        bind.execute(
            sa.text(
                "INSERT INTO identities (id, slug, tenant_id, name, created_at, updated_at)"
                " VALUES (:id, :slug, :tenant_id, :name, now(), now())"
            ),
            {"id": identity_id, "slug": _slug("idn"), "tenant_id": tenant_id, "name": name},
        )
        bind.execute(
            sa.text(
                "INSERT INTO mediators"
                " (id, slug, tenant_id, name, url, identity_id, created_at, updated_at)"
                " VALUES (:id, :slug, :tenant_id, :name, :url, :identity_id, now(), now())"
            ),
            {
                "id": mediator_id,
                "slug": _slug("med"),
                "tenant_id": tenant_id,
                "name": name,
                "url": url,
                "identity_id": identity_id,
            },
        )
        bind.execute(
            sa.text("UPDATE tenants SET mediator_id = :m WHERE id = :t"),
            {"m": mediator_id, "t": tenant_id},
        )
        for table in ("issuers", "verifiers"):
            bind.execute(
                sa.text(f"UPDATE {table} SET mediator_id = :m WHERE tenant_id = :t"),
                {"m": mediator_id, "t": tenant_id},
            )

    op.drop_column("tenants", "mediator_did")
    op.drop_column("tenants", "mediator_url")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("tenants", sa.Column("mediator_url", sa.String(length=2048), nullable=True))
    op.add_column("tenants", sa.Column("mediator_did", sa.String(length=512), nullable=True))
    # The address comes back; the did:web the origin served is not known here.
    op.execute(
        "UPDATE tenants SET mediator_url = mediators.url FROM mediators"
        " WHERE mediators.id = tenants.mediator_id"
    )
    for table in TABLES:
        op.drop_constraint(op.f(f"fk_{table}_mediator_id_mediators"), table, type_="foreignkey")
        op.drop_index(op.f(f"ix_{table}_mediator_id"), table_name=table)
        op.drop_column(table, "mediator_id")
    identities = sa.text("SELECT identity_id FROM mediators")
    op.drop_index(op.f("ix_mediators_tenant_id"), table_name="mediators")
    op.drop_index(op.f("ix_mediators_identity_id"), table_name="mediators")
    ids = [row[0] for row in op.get_bind().execute(identities)]
    op.drop_table("mediators")
    if ids:
        op.get_bind().execute(
            sa.text("DELETE FROM identities WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": ids},
        )
