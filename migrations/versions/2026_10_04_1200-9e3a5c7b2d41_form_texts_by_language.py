"""Form texts by language: a form's name and description, its fields' help and
why it asks for each credential become objects by language (`{"en": …}`).

What was written before was in no stated language; it is kept as English, the
portal's source language, to be corrected or translated from the portal.

Revision ID: 9e3a5c7b2d41
Revises: 4b6e1d9a3c72
Create Date: 2026-10-04 12:00:00.000000

"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e3a5c7b2d41"
down_revision: str | Sequence[str] | None = "4b6e1d9a3c72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _as_texts(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return [
        {**item, key: {"en": item[key]}} if isinstance(item.get(key), str) else item
        for item in items
    ]


def _first(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    out = []
    for item in items:
        value = item.get(key)
        if isinstance(value, dict):
            item = {**item, key: value.get("en") or next(iter(value.values()), "")}
        out.append(item)
    return out


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "forms",
        "name",
        type_=sa.JSON(),
        postgresql_using="json_build_object('en', name)",
    )
    op.alter_column(
        "forms",
        "description",
        type_=sa.JSON(),
        postgresql_using="CASE WHEN description IS NULL THEN NULL "
        "ELSE json_build_object('en', description) END",
    )
    bind = op.get_bind()
    for form_id, fields, credentials in bind.execute(
        sa.text("SELECT id, fields, credentials FROM forms")
    ):
        bind.execute(
            sa.text("UPDATE forms SET fields = :fields, credentials = :credentials WHERE id = :id"),
            {
                "id": form_id,
                "fields": json.dumps(_as_texts(fields, "help")),
                "credentials": json.dumps(_as_texts(credentials, "purpose")),
            },
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    for form_id, fields, credentials in bind.execute(
        sa.text("SELECT id, fields, credentials FROM forms")
    ):
        bind.execute(
            sa.text("UPDATE forms SET fields = :fields, credentials = :credentials WHERE id = :id"),
            {
                "id": form_id,
                "fields": json.dumps(_first(fields, "help")),
                "credentials": json.dumps(_first(credentials, "purpose")),
            },
        )
    op.alter_column(
        "forms",
        "description",
        type_=sa.Text(),
        postgresql_using="coalesce(description->>'en', description->>'es')",
    )
    op.alter_column(
        "forms",
        "name",
        type_=sa.String(length=200),
        postgresql_using="coalesce(name->>'en', name->>'es')",
    )
