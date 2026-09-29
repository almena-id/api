"""Ways in other than email: an account's email becomes optional, provider
accounts can be linked from the account screen, and an empty account can be
left for the one a way in already belongs to.

Revision ID: 6b2e9d4f8a31
Revises: 8e3f5c2a7b14
Create Date: 2026-09-29 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6b2e9d4f8a31"
down_revision: str | Sequence[str] | None = "8e3f5c2a7b14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("users", "email", existing_type=sa.String(length=320), nullable=True)
    op.alter_column("user_identities", "email", existing_type=sa.String(length=320), nullable=True)
    op.add_column("oauth_flows", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_oauth_flows_user_id_users"),
        "oauth_flows",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_table(
        "account_moves",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ticket_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("from_user_id", sa.Uuid(), nullable=False),
        sa.Column("to_user_id", sa.Uuid(), nullable=False),
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
            ["from_user_id"],
            ["users.id"],
            name=op.f("fk_account_moves_from_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_user_id"],
            ["users.id"],
            name=op.f("fk_account_moves_to_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_moves")),
        sa.UniqueConstraint("ticket_hash", name=op.f("uq_account_moves_ticket_hash")),
    )
    op.create_index(
        op.f("ix_account_moves_from_user_id"), "account_moves", ["from_user_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema. Accounts without an email cannot go back: they are removed."""
    op.drop_index(op.f("ix_account_moves_from_user_id"), table_name="account_moves")
    op.drop_table("account_moves")
    op.drop_constraint(op.f("fk_oauth_flows_user_id_users"), "oauth_flows", type_="foreignkey")
    op.drop_column("oauth_flows", "user_id")
    op.execute("DELETE FROM user_identities WHERE email IS NULL")
    op.alter_column("user_identities", "email", existing_type=sa.String(length=320), nullable=False)
    op.execute("DELETE FROM users WHERE email IS NULL")
    op.alter_column("users", "email", existing_type=sa.String(length=320), nullable=False)
