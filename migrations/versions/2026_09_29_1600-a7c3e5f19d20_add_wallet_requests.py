"""Add wallet requests: signing in, or linking, with an Almena wallet.

Revision ID: a7c3e5f19d20
Revises: 6b2e9d4f8a31
Create Date: 2026-09-29 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e5f19d20"
down_revision: str | Sequence[str] | None = "6b2e9d4f8a31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "wallet_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("poll_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("locale", sa.String(length=8), nullable=False),
        sa.Column("did", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            ["user_id"],
            ["users.id"],
            name=op.f("fk_wallet_requests_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallet_requests")),
        sa.UniqueConstraint("poll_hash", name=op.f("uq_wallet_requests_poll_hash")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("wallet_requests")
