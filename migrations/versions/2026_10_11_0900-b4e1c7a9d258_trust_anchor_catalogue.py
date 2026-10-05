"""The trust anchor's catalogue: Almena's fields and credential types as data.

`custom_fields` becomes `catalog_fields`, every tenant's fields, with the
standard each is named after and its category; value domains, categories and
credential types get tables of their own. An existing root becomes the trust
anchor: named "Almena Trust Anchor" if it kept the default name, and given
Almena's catalogue (``assets/catalogue-v1.json``), which until now was code. A
root made later gets it from `registry-api init-root`.

Revision ID: b4e1c7a9d258
Revises: 2c8e6a4f9b13
Create Date: 2026-10-11 09:00:00.000000

"""

import json
import secrets
import string
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e1c7a9d258"
down_revision: str | Sequence[str] | None = "2c8e6a4f9b13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED = files("registry_api") / "assets" / "catalogue-v1.json"
ANCHOR = "Almena Trust Anchor"
RENAMED = [
    ("pk_custom_fields", "pk_catalog_fields"),
    ("uq_custom_fields_slug", "uq_catalog_fields_slug"),
    ("uq_custom_fields_tenant_id", "uq_catalog_fields_tenant_id"),
    ("fk_custom_fields_tenant_id_tenants", "fk_catalog_fields_tenant_id_tenants"),
]


def _timestamps() -> list[sa.Column[Any]]:
    now = sa.text("now()")
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
    ]


def _anchored(table: str, *columns: sa.Column[Any], unique: Sequence[str] = ("key",)) -> None:
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *columns,
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{table}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint("tenant_id", *unique, name=op.f(f"uq_{table}_tenant_id")),
    )
    op.create_index(op.f(f"ix_{table}_tenant_id"), table, ["tenant_id"], unique=False)


def _slug(prefix: str) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return prefix + "_" + "".join(secrets.choice(alphabet) for _ in range(12))


def upgrade() -> None:
    op.rename_table("custom_fields", "catalog_fields")
    for old, new in RENAMED:
        op.execute(f"ALTER TABLE catalog_fields RENAME CONSTRAINT {old} TO {new}")
    op.execute("ALTER INDEX ix_custom_fields_tenant_id RENAME TO ix_catalog_fields_tenant_id")
    op.add_column(
        "catalog_fields", sa.Column("source", sa.String(200), server_default="", nullable=False)
    )
    op.add_column(
        "catalog_fields", sa.Column("category", sa.String(32), server_default="", nullable=False)
    )
    _anchored(
        "catalog_domains",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("codes", sa.JSON(), nullable=False),
    )
    _anchored(
        "catalog_categories",
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        unique=("kind", "key"),
    )
    _anchored(
        "catalog_types",
        sa.Column("slug", sa.String(32), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("descriptions", sa.JSON(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("claims", sa.JSON(), nullable=False),
        sa.Column("issuance", sa.String(16), server_default="almena", nullable=False),
        sa.Column("vct", sa.Text(), nullable=True),
        sa.Column("w3c_type", sa.String(100), nullable=True),
        sa.Column("mdoc_doctype", sa.String(200), nullable=True),
    )
    op.create_unique_constraint(op.f("uq_catalog_types_slug"), "catalog_types", ["slug"])
    _seed()


def _seed() -> None:
    """The existing root, if any, becomes the anchor, with Almena's catalogue."""
    bind = op.get_bind()
    root = bind.execute(sa.text("SELECT id, name, identity_id FROM tenants WHERE root")).first()
    if root is None:
        return
    anchor, name, identity = root
    if name == "Almena":
        renamed = {"name": ANCHOR, "id": anchor}
        bind.execute(sa.text("UPDATE tenants SET name = :name WHERE id = :id"), renamed)
        if identity is not None:
            bind.execute(
                sa.text("UPDATE identities SET name = :name WHERE id = :id AND name = 'Almena'"),
                {"name": ANCHOR, "id": identity},
            )
    data = json.loads(SEED.read_text(encoding="utf-8"))
    tick = iter(datetime.now(UTC) + timedelta(microseconds=n) for n in range(10_000))

    def rows(entries: list[dict[str, Any]], **more: Any) -> list[dict[str, Any]]:
        return [
            {"id": uuid.uuid4(), "tenant_id": anchor, "created_at": next(tick), **more, **entry}
            for entry in entries
        ]

    def table(name: str, *columns: str) -> sa.TableClause:
        return sa.table(
            name,
            sa.column("id", sa.Uuid()),
            sa.column("tenant_id", sa.Uuid()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            *(sa.column(each, sa.JSON() if each in JSON else sa.String()) for each in columns),
        )

    taken = {
        key
        for (key,) in bind.execute(
            sa.text("SELECT key FROM catalog_fields WHERE tenant_id = :id"), {"id": anchor}
        )
    }
    op.bulk_insert(table("catalog_categories", "kind", "key", "labels"), rows(data["categories"]))
    op.bulk_insert(
        table("catalog_domains", "key", "labels", "source", "codes"), rows(data["domains"])
    )
    fields = [field for field in data["fields"] if field["key"] not in taken]
    op.bulk_insert(
        table(
            "catalog_fields", "slug", "key", "type", "labels", "source", "category", "definition"
        ),
        [{**row, "slug": _slug("fld")} for row in rows(fields)],
    )
    columns = ("slug", "key", "labels", "descriptions", "category", "source", "claims")
    extra = ("issuance", "vct", "w3c_type", "mdoc_doctype")
    op.bulk_insert(
        table("catalog_types", *columns, *extra),
        [
            {"issuance": "almena", "vct": None, "w3c_type": None, "mdoc_doctype": None, **row}
            | {"slug": _slug("cty")}
            for row in rows(data["credential_types"])
        ],
    )


JSON = {"labels", "codes", "definition", "descriptions", "claims"}


def downgrade() -> None:
    op.drop_constraint(op.f("uq_catalog_types_slug"), "catalog_types", type_="unique")
    for table in ("catalog_types", "catalog_categories", "catalog_domains"):
        op.drop_index(op.f(f"ix_{table}_tenant_id"), table_name=table)
        op.drop_table(table)
    # The anchor's fields were Almena's catalogue, code again from here back.
    op.execute(
        "DELETE FROM catalog_fields WHERE category <> '' "
        "AND tenant_id IN (SELECT id FROM tenants WHERE root)"
    )
    op.drop_column("catalog_fields", "category")
    op.drop_column("catalog_fields", "source")
    op.execute("ALTER INDEX ix_catalog_fields_tenant_id RENAME TO ix_custom_fields_tenant_id")
    for old, new in RENAMED:
        op.execute(f"ALTER TABLE catalog_fields RENAME CONSTRAINT {new} TO {old}")
    op.rename_table("catalog_fields", "custom_fields")
