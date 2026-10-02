"""Identities' messaging key: an issuer's or verifier's X25519 public key, a
multikey listed under `keyAgreement`, made when its signer signs it. The
private key is in the vault.

Revision ID: 6c2f8a1d4e93
Revises: 9e3a5c7b2d41
Create Date: 2026-10-05 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6c2f8a1d4e93"
down_revision: str | Sequence[str] | None = "9e3a5c7b2d41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("identities", sa.Column("agreement_key", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("identities", "agreement_key")
