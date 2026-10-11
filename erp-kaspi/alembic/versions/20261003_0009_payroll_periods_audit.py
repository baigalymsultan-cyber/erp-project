"""Audit trigger on payroll_periods (A-1, FINANCE-OPS-001)

Revision ID: 0009_payroll_periods_audit
Revises: 0008_provenance_delete_guard
Create Date: 2026-10-03

A-1: payroll_periods.status is a financial control gate (blocks
calculate_payroll when CLOSED/PAID), but status transitions were not
in the audit trail — the audit trigger list in database/schema.sql
§7.3 omitted payroll_periods.

This revision adds trg_payroll_periods_audit (AFTER INSERT OR UPDATE
OR DELETE, using the existing fn_audit()) — the same pattern as the
other 19 audited tables. No data is changed.

database/schema.sql stays the source of truth (ADR-011); the trigger
name matches the schema.sql DO block in §7.3.

Downgrade drops the trigger only; fn_audit() itself is left in place.
"""

from __future__ import annotations

from alembic import op

revision = "0009_payroll_periods_audit"
down_revision = "0008_provenance_delete_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_payroll_periods_audit ON payroll_periods;
        CREATE TRIGGER trg_payroll_periods_audit
            AFTER INSERT OR UPDATE OR DELETE ON payroll_periods
            FOR EACH ROW EXECUTE FUNCTION fn_audit();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_payroll_periods_audit ON payroll_periods;")
