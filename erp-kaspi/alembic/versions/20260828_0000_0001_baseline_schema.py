"""baseline schema — applies database/schema.sql

Revision ID: 0001_baseline_schema
Revises: None
Create Date: 2026-08-28

MASTER PROMPT / ADR references
------------------------------
§7, §83, §84
    STEP 1 produced the complete physical schema in database/schema.sql:
    32 tables, 16 functions, 64 triggers, 120 indexes, 108 CHECK constraints,
    62 foreign keys, 3 EXCLUDE constraints, 5 views and idempotent seed data.

ADR-011 (schema.sql is the source of truth)
    This baseline revision does not re-declare those objects in Python. It
    executes the reviewed, audited SQL file verbatim, which guarantees that a
    database created by Alembic is byte-for-byte identical to a database
    created by scripts/apply_schema.sh. Re-implementing 2 000 lines of DDL as
    op.create_table() calls would silently drop everything Alembic cannot
    express (trigger bodies, EXCLUDE constraints, GENERATED columns, partial
    indexes, view definitions) — exactly the omission that §1 forbids.

§87 (re-runnable)
    schema.sql is fully idempotent, so this revision can also be applied on
    top of a database that was already provisioned by scripts/apply_schema.sh;
    the objects are then left untouched and only alembic_version is written.

§16 (financial data is never destroyed)
    downgrade() would drop the ledger together with every other object, which
    §16 forbids as a routine operation. It therefore refuses to run unless the
    operator explicitly opts in through the environment variable
    ERP_ALLOW_BASELINE_DOWNGRADE=1 — the same escape-hatch pattern used by the
    database trigger fn_forbid_delete() (app.allow_financial_delete).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0001_baseline_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Repository-relative location of the STEP 1 deliverable.
SCHEMA_FILE = Path(__file__).resolve().parents[2] / "database" / "schema.sql"

#: psql client meta-commands (backslash commands) that appear in schema.sql.
#:
#: BUG-002, found by actually running `alembic upgrade head` and not by review:
#: schema.sql line 30 contains `\set ON_ERROR_STOP on`. That directive is
#: interpreted by the *psql client*, it is not SQL, and PostgreSQL rejects it
#: with `syntax error at or near "\"` when the file is sent through a driver
#: (psycopg / Alembic / any ORM).
#:
#: The directive is deliberately KEPT in schema.sql — it is what makes
#: `psql -f database/schema.sql` abort on the first error, and removing it
#: would weaken the psql path (§1 forbids silently dropping a requirement).
#: Instead the driver path strips the client-only lines before execution.
#: `\set ON_ERROR_STOP on` has no effect to reproduce here: Alembic already
#: runs the whole revision inside one transaction, so any error aborts and
#: rolls back the entire schema — a stronger guarantee than psql's.
_PSQL_META_COMMAND_RE = re.compile(r"^[ \t]*\\[A-Za-z].*$", re.MULTILINE)


def _strip_psql_meta_commands(sql: str) -> tuple[str, list[str]]:
    """
    Remove psql client meta-commands from *sql*.

    Returns the driver-executable SQL and the list of stripped lines, so that
    the caller can assert on exactly what was removed.

    Only whole lines starting with a backslash followed by a letter are
    removed. A backslash inside a string literal or a function body is never
    at the start of a line in schema.sql, and E'\\n'-style escapes are not
    line-initial either, so this cannot damage real SQL. The assertion in
    tests/test_alembic_baseline.py::test_only_known_meta_commands_are_stripped
    pins the removed set to the single known directive.
    """
    stripped: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        stripped.append(match.group(0).strip())
        return ""

    return _PSQL_META_COMMAND_RE.sub(_replace, sql), stripped

#: Objects created by SECTION 8 of schema.sql (§63).
_VIEWS: tuple[str, ...] = (
    "v_kaspi_reconciliation",
    "v_order_costing",
    "v_work_entries_detail",
    "v_payroll_due",
    "v_ip_financial_summary",
    "v_counterparty_balances",
    "v_material_stock",
    "v_material_availability",
    "v_wip_by_order",
    "v_finished_goods_stock",
)

#: Tables created by SECTIONS 3 and 4 of schema.sql, in reverse dependency
#: order. CASCADE is still used because the ledger contains circular foreign
#: keys added in SECTION 5 (orders ↔ raw_kaspi_payloads, kaspi_payouts ↔
#: operations).
_TABLES: tuple[str, ...] = (
    "payroll_payments",
    "payroll_summary",
    "work_entries",
    "rates_history",
    "transfers",
    "operations",
    "kaspi_payout_items",
    "kaspi_payouts",
    "sync_log",
    "raw_kaspi_payloads",
    "order_materials",
    "order_items",
    "order_status_history",
    "orders",
    "kaspi_api_credentials",
    "import_batches",
    "tax_rates_history",
    "accounts",
    "materials",
    "products",
    "audit_log",
    "user_roles",
    "roles",
    "users",
    "payroll_periods",
    "work_operation_types",
    "employees",
    "counterparties",
    "categories",
    "operation_types",
    "ip_entities",
    "settlement_links",
    "raw_bank_statements",
    "bank_statement_lines",
    "bank_fee_knp_codes",
    "material_reservations",
    "stock_movements",
    "production_output",
)

#: Functions created by SECTION 2 of schema.sql.
_FUNCTIONS: tuple[str, ...] = (
    "fn_current_app_user()",
    "fn_set_updated_at()",
    "fn_forbid_delete()",
    "fn_audit()",
    "fn_work_entry_freeze_rate()",
    "fn_work_entry_protect_rate()",
    "fn_order_material_freeze_price()",
    "fn_order_material_protect_price()",
    "fn_log_order_status_change()",
    "fn_check_operation_category()",
    "fn_check_operation_sign()",
    "fn_protect_posted_operation()",
    "fn_protect_posted_transfer()",
    "fn_transfer_ledger_pair_is_valid(BIGINT)",
    "fn_validate_transfer_ledger_pair()",
    "fn_protect_posted_ledger_operation()",
    "fn_payroll_payment_no_overpay()",
    "fn_settlement_links_integrity()",
    "fn_settlement_links_immutable()",
    "fn_bank_statement_lines_immutable()",
    "fn_stock_movements_integrity()",
    "fn_stock_movements_immutable()",
    "fn_reservations_ip()",
    "fn_production_output_immutable()",
    "fn_production_output_ip()",
)


def upgrade() -> None:
    """Apply database/schema.sql verbatim."""
    if not SCHEMA_FILE.is_file():
        raise RuntimeError(
            f"database/schema.sql not found at {SCHEMA_FILE}. "
            "The baseline revision cannot be applied without the STEP 1 DDL."
        )

    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    if "CREATE TABLE IF NOT EXISTS ip_entities" not in sql:
        raise RuntimeError(
            f"{SCHEMA_FILE} does not look like the ERP schema "
            "(marker table ip_entities not found)."
        )

    # BUG-002: strip psql client meta-commands, which a driver cannot execute.
    executable_sql, stripped = _strip_psql_meta_commands(sql)
    unexpected = [line for line in stripped if line != r"\set ON_ERROR_STOP on"]
    if unexpected:
        raise RuntimeError(
            "database/schema.sql contains psql meta-commands that this "
            "revision does not know how to handle, so applying the file "
            "through a driver would silently skip them: "
            f"{unexpected}. Either express them as SQL or extend "
            "_strip_psql_meta_commands() deliberately."
        )

    # psycopg executes a multi-statement string as one implicit block, which is
    # what we want: the whole schema is applied inside the migration
    # transaction and rolls back completely on any error.
    op.execute(executable_sql)


def downgrade() -> None:
    """
    Drop every object created by the baseline revision.

    Refuses to run unless ERP_ALLOW_BASELINE_DOWNGRADE=1 is set, because this
    destroys the financial ledger (§16).
    """
    if os.environ.get("ERP_ALLOW_BASELINE_DOWNGRADE") != "1":
        raise RuntimeError(
            "Refusing to downgrade the baseline revision: this drops the "
            "financial ledger, which MASTER PROMPT §16 forbids. "
            "Set ERP_ALLOW_BASELINE_DOWNGRADE=1 to override deliberately, and "
            "take a verified backup first."
        )

    for view in _VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view} CASCADE")

    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    for function in _FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function} CASCADE")

    # btree_gist is required by the EXCLUDE constraints (§31, §34, §75). It is
    # intentionally NOT dropped: the extension may be shared with other
    # schemas in the same database.
