"""Drop redundant indexes covered by UNIQUE constraints.

Revision ID: 0019_drop_redundant_indexes
Revises: 0018_security_hardening
Create Date: 2026-10-04

P2-2 (2026-10-03) added plain btree indexes on columns that already have
UNIQUE constraints. A unique btree serves every lookup the plain index
would, so the duplicates are pure write-amplification:

- idx_payroll_payments_operation  → covered by uq_payroll_payments_operation
- idx_transfers_outbound_op       → covered by uq_transfers_outbound
- idx_transfers_inbound_op        → covered by uq_transfers_inbound

Declaring them in the models would enshrine the waste; dropping is the
correct fix (test_indexes_declared goes GREEN either way).
"""

from __future__ import annotations

from alembic import op

revision = "0019_drop_redundant_indexes"
down_revision = "0018_security_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_payroll_payments_operation;")
    op.execute("DROP INDEX IF EXISTS idx_transfers_outbound_op;")
    op.execute("DROP INDEX IF EXISTS idx_transfers_inbound_op;")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_payroll_payments_operation "
        "ON payroll_payments (operation_id) WHERE operation_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfers_outbound_op ON transfers (outbound_operation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfers_inbound_op ON transfers (inbound_operation_id);"
    )
