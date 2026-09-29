"""Add did:webvh logs: an identity's DID and its signed log, and what a wallet
is asked to sign.

Identities made before this had a did:web DID derived from their slug and kept
nowhere; they become pending, to be signed again (nothing is in production).

Revision ID: c4d8e2a6b913
Revises: a7c3e5f19d20
Create Date: 2026-09-30 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d8e2a6b913"
down_revision: str | Sequence[str] | None = "a7c3e5f19d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("identities", sa.Column("did", sa.String(length=255), nullable=True))
    op.create_unique_constraint(op.f("uq_identities_did"), "identities", ["did"])
    op.add_column("wallet_requests", sa.Column("payload", sa.Text(), nullable=True))
    op.create_table(
        "did_log_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("entry", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["identities.id"],
            name=op.f("fk_did_log_entries_identity_id_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_did_log_entries")),
        sa.UniqueConstraint("identity_id", "version", name=op.f("uq_did_log_entries_identity_id")),
    )
    op.create_index(
        op.f("ix_did_log_entries_identity_id"), "did_log_entries", ["identity_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_did_log_entries_identity_id"), table_name="did_log_entries")
    op.drop_table("did_log_entries")
    op.drop_column("wallet_requests", "payload")
    op.drop_constraint(op.f("uq_identities_did"), "identities", type_="unique")
    op.drop_column("identities", "did")
