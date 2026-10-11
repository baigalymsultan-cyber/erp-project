"""DELETE guard on provenance tables (TD-019 step 2, FINANCE-OPS-001)

Revision ID: 0008_provenance_delete_guard
Revises: 0007_posted_ledger_immutability
Create Date: 2026-10-01

TD-019: operations/transfers keep ON DELETE SET NULL links to
raw_kaspi_payloads and import_batches. Those provenance tables had no
DELETE guard, so a plain DELETE (by the application role, or the old
test purge order) silently destroyed the provenance chain behind
Absolute Rule 12 ("every significant amount must have provenance").

This revision adds the existing fn_forbid_delete() BEFORE DELETE trigger
to raw_kaspi_payloads and import_batches -- the same guard (and the same
migrator-only maintenance hatch) already used for the financial tables.
No data is changed; the application never DELETEs from these tables
(verified by code search), so no pre-check is needed.

database/schema.sql stays the source of truth (ADR-011); the trigger
names below match the schema.sql DO block in §7.2, which installs
trg_<table>_forbid_delete for every table in its list.

Downgrade drops the two new triggers only; fn_forbid_delete() itself
is left in place (owned by the baseline/earlier revisions).
"""

from __future__ import annotations

from alembic import op

revision = "0008_provenance_delete_guard"
down_revision = "0007_posted_ledger_immutability"
branch_labels = None
depends_on = None

# Must stay in sync with the v_tables array in database/schema.sql §7.2.
PROVENANCE_TABLES: tuple[str, ...] = ("raw_kaspi_payloads", "import_batches")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    for table in PROVENANCE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_forbid_delete ON public.{table}")
        op.execute(
            f"CREATE TRIGGER trg_{table}_forbid_delete "
            f"BEFORE DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION fn_forbid_delete()"
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    for table in PROVENANCE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_forbid_delete ON public.{table}")
