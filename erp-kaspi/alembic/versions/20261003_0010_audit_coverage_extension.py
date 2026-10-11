"""Audit triggers on provenance, order items, reference data (A-2/A-3/A-4)

Revision ID: 0010_audit_coverage_extension
Revises: 0009_payroll_periods_audit
Create Date: 2026-10-03

A-2: import_batches / raw_kaspi_payloads — provenance tables with DELETE
guards (TD-019) but lifecycle transitions (SUCCESS/PARTIAL/FAILED) were
not in the audit trail.

A-3: order_items / order_materials / order_status_history — line-item
detail; parent orders row was already audited.

A-4: categories / counterparties / operation_types / work_operation_types —
reference data; changes (e.g., operation type direction flip) alter
financial semantics silently.

This revision adds trg_<table>_audit (AFTER INSERT OR UPDATE OR DELETE,
using the existing fn_audit()) for all 9 tables — the same pattern as
the other 20 audited tables. No data is changed.

database/schema.sql stays the source of truth (ADR-011); trigger names
match the schema.sql DO block in §7.3.

Downgrade drops the 9 new triggers only; fn_audit() is left in place.
"""

from __future__ import annotations

from alembic import op

revision = "0010_audit_coverage_extension"
down_revision = "0009_payroll_periods_audit"
branch_labels = None
depends_on = None

_TABLES = [
    "import_batches",
    "raw_kaspi_payloads",
    "order_items",
    "order_materials",
    "order_status_history",
    "categories",
    "counterparties",
    "operation_types",
    "work_operation_types",
]


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"""
            DROP TRIGGER IF EXISTS trg_{table}_audit ON {table};
            CREATE TRIGGER trg_{table}_audit
                AFTER INSERT OR UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION fn_audit();
            """
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_audit ON {table};")
