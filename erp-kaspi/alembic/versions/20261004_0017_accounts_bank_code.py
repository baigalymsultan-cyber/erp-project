"""Add bank_code to accounts (G-BANK-1)

Revision ID: 0017_accounts_bank_code
Revises: 0016_settlement_links_integrity
Create Date: 2026-10-04

G-BANK-1: accounts table has no bank identifier. Two accounts in Halyk
are indistinguishable programmatically. Adds nullable bank_code
(HALYK, FREEDOM, KASPI, etc.) for bank-type accounts.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_accounts_bank_code"
down_revision = "0016_settlement_links_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotency guard: the baseline revision applies database/schema.sql
    # verbatim, which already contains bank_code for fresh databases.
    # Without the guard, `alembic upgrade head` on a fresh DB fails with
    # DuplicateColumn at this revision.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("accounts")}
    if "bank_code" not in columns:
        op.add_column(
            "accounts",
            sa.Column("bank_code", sa.VARCHAR(20), nullable=True),
        )
    op.execute(
        """
        COMMENT ON COLUMN accounts.bank_code IS
        'Bank identifier for type=BANK accounts: HALYK, FREEDOM, KASPI, etc. '
        'NULL for non-bank accounts. (G-BANK-1)';
        """
    )


def downgrade() -> None:
    op.drop_column("accounts", "bank_code")
