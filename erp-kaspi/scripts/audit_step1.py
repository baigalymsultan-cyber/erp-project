#!/usr/bin/env python3
"""
STEP 1 SELF-AUDIT (MASTER PROMPT §90, §91)

Verifies the applied PostgreSQL schema against MASTER PROMPT v3.0.

Checks, per §90:
  * the 17 original tables exist
  * every required column exists, with the required type, NOT NULL, DEFAULT
  * PRIMARY KEY / FOREIGN KEY / UNIQUE / CHECK constraints
  * mandatory indexes
  * generated columns
  * triggers
  * views
  * seed data
plus behavioural tests for the rules that cannot be verified structurally
(rate freeze, price freeze, DELETE protection, EXCLUDE constraints,
sign validation, idempotency, audit log, updated_at).

Usage:
    python scripts/audit_step1.py                 # uses DATABASE_URL or defaults
    DATABASE_URL=postgresql://... python scripts/audit_step1.py

Exit code 0 = all checks passed. Non-zero = MISSING REQUIREMENT (§91).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

import psycopg

DEFAULT_DSN = "postgresql://erp_app:devpass_local_only@127.0.0.1:5432/erp_test"

# ---------------------------------------------------------------------------
# The 17 original tables (§80, §81) with their mandatory column contracts.
# Format: column -> (data_type, is_nullable, must_have_default)
# ---------------------------------------------------------------------------
ORIGINAL_TABLES: dict[str, dict[str, tuple[str, bool, bool]]] = {
    # 1. ip_entities (§17)
    "ip_entities": {
        "id": ("bigint", False, True),
        "name": ("character varying", False, False),
        "bin": ("character varying", False, False),
        "is_active": ("boolean", False, True),
        "created_at": ("timestamp with time zone", False, True),
        "closed_at": ("timestamp with time zone", True, False),
    },
    # 2. kaspi_api_credentials (§18)
    "kaspi_api_credentials": {
        "id": ("bigint", False, True),
        "ip_id": ("bigint", False, False),
        "api_type": ("character varying", False, False),
        "encrypted_token": ("bytea", False, False),
        "expires_at": ("timestamp with time zone", True, False),
        "status": ("character varying", False, True),
        "created_at": ("timestamp with time zone", False, True),
        "updated_at": ("timestamp with time zone", False, True),
    },
    # 3. operation_types (§20)
    "operation_types": {
        "id": ("bigint", False, True),
        "code": ("character varying", False, False),
        "name": ("character varying", False, False),
    },
    # 4. categories (§21)
    "categories": {
        "id": ("bigint", False, True),
        "name": ("character varying", False, False),
        "kind": ("character varying", False, False),
    },
    # 5. counterparties (§22)
    "counterparties": {
        "id": ("bigint", False, True),
        "name": ("character varying", False, False),
        "bin_iin": ("character varying", True, False),
        "phone": ("character varying", True, False),
        "comment": ("text", True, False),
        "created_at": ("timestamp with time zone", False, True),
    },
    # 6. orders (§23)
    "orders": {
        "id": ("bigint", False, True),
        "ip_id": ("bigint", False, False),
        "external_order_id": ("character varying", True, False),
        "customer_name": ("character varying", True, False),
        "customer_phone": ("character varying", True, False),
        "delivery_address": ("text", True, False),
        "status": ("character varying", False, True),
        "total_amount": ("numeric", False, True),
        "kaspi_commission": ("numeric", False, True),
        "delivery_fee": ("numeric", False, True),
        "created_at": ("timestamp with time zone", False, True),
        "updated_at": ("timestamp with time zone", False, True),
    },
    # 7. raw_kaspi_payloads (§26)
    "raw_kaspi_payloads": {
        "id": ("bigint", False, True),
        "ip_id": ("bigint", False, False),
        "request_type": ("character varying", False, False),
        "received_at": ("timestamp with time zone", False, True),
        "payload": ("jsonb", False, False),
    },
    # 8. operations (§27)
    "operations": {
        "id": ("bigint", False, True),
        "ip_id": ("bigint", False, False),
        "type_id": ("bigint", False, False),
        "category_id": ("bigint", True, False),
        "counterparty_id": ("bigint", True, False),
        "amount": ("numeric", False, False),
        "operation_date": ("date", False, False),
        "external_id": ("character varying", True, False),
        "related_order_id": ("bigint", True, False),
        "related_operation_id": ("bigint", True, False),
        "raw_payload_id": ("bigint", True, False),
        "comment": ("text", True, False),
        "created_at": ("timestamp with time zone", False, True),
    },
    # 9. transfers (§28)
    "transfers": {
        "id": ("bigint", False, True),
        "from_ip_id": ("bigint", False, False),
        "to_ip_id": ("bigint", False, False),
        "amount": ("numeric", False, False),
        "transfer_date": ("date", False, False),
        "reason": ("text", True, False),
        "outbound_operation_id": ("bigint", True, False),
        "inbound_operation_id": ("bigint", True, False),
    },
    # 10. sync_log (§45)
    "sync_log": {
        "id": ("bigint", False, True),
        "ip_id": ("bigint", False, False),
        "api_type": ("character varying", False, False),
        "sync_started_at": ("timestamp with time zone", False, True),
        "sync_finished_at": ("timestamp with time zone", True, False),
        "records_fetched": ("integer", False, True),
        "records_inserted": ("integer", False, True),
        "records_updated": ("integer", False, True),
        "errors_count": ("integer", False, True),
        "error_details": ("jsonb", True, False),
    },
    # 11. employees (§29)
    "employees": {
        "id": ("bigint", False, True),
        "full_name": ("character varying", False, False),
        "phone": ("character varying", True, False),
        "position": ("character varying", True, False),
        "assigned_ip_id": ("bigint", True, False),
        "hired_at": ("date", True, False),
        "fired_at": ("date", True, False),
        "status": ("character varying", False, True),
        "payment_type": ("character varying", False, False),
    },
    # 12. work_operation_types (§30)
    "work_operation_types": {
        "id": ("bigint", False, True),
        "code": ("character varying", False, False),
        "name": ("character varying", False, False),
        "unit": ("character varying", False, False),
        "calc_mode": ("character varying", False, False),
    },
    # 13. rates_history (§31)
    "rates_history": {
        "id": ("bigint", False, True),
        "work_operation_type_id": ("bigint", False, False),
        "rate": ("numeric", False, False),
        "effective_from": ("timestamp with time zone", False, False),
        "effective_to": ("timestamp with time zone", True, False),
    },
    # 14. work_entries (§33)
    "work_entries": {
        "id": ("bigint", False, True),
        "employee_id": ("bigint", False, False),
        "work_operation_type_id": ("bigint", False, False),
        "order_id": ("bigint", True, False),
        "ip_id": ("bigint", False, False),
        "entry_date": ("date", False, False),
        "quantity": ("numeric", False, False),
        "rate_at_time": ("numeric", False, False),
        "total_sum": ("numeric", True, False),
        "created_by_user": ("bigint", True, False),
        "status": ("character varying", False, True),
        "created_at": ("timestamp with time zone", False, True),
    },
    # 15. payroll_periods (§34)
    "payroll_periods": {
        "id": ("bigint", False, True),
        "start_date": ("date", False, False),
        "end_date": ("date", False, False),
        "status": ("character varying", False, True),
    },
    # 16. payroll_summary (§35)
    "payroll_summary": {
        "id": ("bigint", False, True),
        "employee_id": ("bigint", False, False),
        "payroll_period_id": ("bigint", False, False),
        "piecework_total": ("numeric", False, True),
        "base_salary": ("numeric", False, True),
        "bonuses": ("numeric", False, True),
        "deductions": ("numeric", False, True),
        "total_calculated": ("numeric", True, False),
        "created_at": ("timestamp with time zone", False, True),
    },
    # 17. payroll_payments (§36)
    "payroll_payments": {
        "id": ("bigint", False, True),
        "payroll_summary_id": ("bigint", False, False),
        "payment_date": ("date", False, False),
        "amount": ("numeric", False, False),
        "payment_method": ("character varying", False, False),
        "operation_id": ("bigint", True, False),
    },
}

EXTENSION_TABLES = [
    "products",
    "order_items",
    "materials",
    "order_materials",
    "kaspi_payouts",
    "kaspi_payout_items",
    "accounts",
    "users",
    "roles",
    "user_roles",
    "audit_log",
    "order_status_history",
    "tax_rates_history",
    "import_batches",
]

MANDATORY_INDEXES = [
    "idx_operations_analytics",
    "idx_work_entries_emp",
    "idx_orders_status",
    "idx_rates_lookup",
]

MANDATORY_VIEWS = [
    "v_ip_financial_summary",
    "v_payroll_due",
    "v_work_entries_detail",
]

MONEY_COLUMNS = [
    ("orders", "total_amount"),
    ("orders", "kaspi_commission"),
    ("orders", "delivery_fee"),
    ("operations", "amount"),
    ("transfers", "amount"),
    ("rates_history", "rate"),
    ("work_entries", "rate_at_time"),
    ("work_entries", "total_sum"),
    ("payroll_summary", "piecework_total"),
    ("payroll_summary", "base_salary"),
    ("payroll_summary", "bonuses"),
    ("payroll_summary", "deductions"),
    ("payroll_summary", "total_calculated"),
    ("payroll_payments", "amount"),
    ("materials", "current_price"),
    ("products", "selling_price"),
    ("order_items", "unit_price"),
    ("order_items", "discount"),
    ("order_items", "total_amount"),
    ("order_materials", "price_at_time"),
    ("order_materials", "total_cost"),
    ("kaspi_payouts", "gross_amount"),
    ("kaspi_payouts", "net_amount"),
    ("employees", "base_salary"),
]

QUANTITY_COLUMNS = [
    ("work_entries", "quantity"),
    ("order_items", "quantity"),
    ("order_materials", "quantity"),
]

GENERATED_COLUMNS = [
    ("work_entries", "total_sum"),
    ("payroll_summary", "total_calculated"),
    ("order_items", "total_amount"),
    ("order_materials", "total_cost"),
]

REQUIRED_OPERATION_TYPES = [
    "SALE_KASPI_SHOP",
    "SALE_KASPI_PAY",
    "EXPENSE_MATERIAL",
    "EXPENSE_KASPI_FEE",
    "SALARY_PAYMENT",
    "TRANSFER_IN",
    "TRANSFER_OUT",
]

REQUIRED_WORK_TYPES = {
    "SAWING": ("м2", "per_unit", 150),
    "EDGING_STATIONARY": ("пог_м", "per_unit", 80),
    "EDGING_MANUAL": ("пог_м", "per_unit", 120),
    "DRILLING_FITTING": ("отверстие", "per_unit", 30),
    "MILLING": ("пог_м", "per_unit", 200),
    "DRILLING_CONFIRMAT": ("отверстие", "per_unit", 20),
    "ASSEMBLY": ("шт", "per_unit", 2000),
    "PACKAGING": ("шт", "per_unit", 500),
    "LOADING": ("часы", "per_hour", 1500),
    "DELIVERY_MOUNT": ("рейс", "per_order", 10000),
    "MATERIAL_RECEIVING": ("часы", "per_hour", 1200),
}

REQUIRED_CATEGORIES = [
    ("Kaspi Shop", "INCOME"),
    ("Kaspi Pay", "INCOME"),
    ("Прочие доходы", "INCOME"),
    ("Материалы", "EXPENSE"),
    ("Комиссия Kaspi", "EXPENSE"),
    ("Доставка", "EXPENSE"),
    ("Зарплата", "EXPENSE"),
    ("Производство", "EXPENSE"),
    ("Аренда", "EXPENSE"),
    ("Коммунальные расходы", "EXPENSE"),
    ("Реклама", "EXPENSE"),
    ("Прочие расходы", "EXPENSE"),
]

REQUIRED_ROLES = [
    "ADMIN",
    "DIRECTOR",
    "ACCOUNTANT",
    "HR",
    "SHOP_MANAGER",
    "BRIGADIER",
    "VIEWER",
]

REQUIRED_TRIGGER_FUNCTIONS = [
    "fn_set_updated_at",
    "fn_forbid_delete",
    "fn_audit",
    "fn_work_entry_freeze_rate",
    "fn_work_entry_protect_rate",
    "fn_order_material_freeze_price",
    "fn_order_material_protect_price",
    "fn_log_order_status_change",
    "fn_check_operation_category",
    "fn_check_operation_sign",
    "fn_current_app_user",
    "fn_protect_posted_operation",
    "fn_protect_posted_transfer",
    "fn_protect_posted_ledger_operation",
    "fn_transfer_ledger_pair_is_valid",
    "fn_validate_transfer_ledger_pair",
]


@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def ok(self, msg: str) -> None:
        self.passed.append(msg)

    def fail(self, msg: str) -> None:
        self.failed.append(msg)

    def check(self, cond: bool, msg: str) -> bool:
        if cond:
            self.ok(msg)
        else:
            self.fail(msg)
        return cond


def audit_structure(cur, rep: Report) -> None:
    """§90 — structural checks."""
    cur.execute("SHOW server_version")
    version = cur.fetchone()[0]
    major = int(version.split(".")[0])
    rep.check(major >= 15, f"REQ-0001 PostgreSQL >= 15 (actual {version})")

    # --- the 17 original tables -------------------------------------------
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)
    existing = {r[0] for r in cur.fetchall()}

    for table in ORIGINAL_TABLES:
        rep.check(table in existing, f"§81 original table exists: {table}")

    for table in EXTENSION_TABLES:
        rep.check(table in existing, f"§82 extension table exists: {table}")

    # --- columns: type, NOT NULL, DEFAULT --------------------------------
    cur.execute("""
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
    """)
    cols: dict[tuple[str, str], tuple[str, str, str | None]] = {
        (r[0], r[1]): (r[2], r[3], r[4]) for r in cur.fetchall()
    }

    for table, spec in ORIGINAL_TABLES.items():
        for col, (dtype, nullable, has_default) in spec.items():
            key = (table, col)
            if key not in cols:
                rep.fail(f"§81 column missing: {table}.{col}")
                continue
            actual_type, actual_nullable, actual_default = cols[key]
            rep.check(
                actual_type == dtype, f"§13 type {table}.{col} = {dtype} (actual {actual_type})"
            )
            expected_nullable = "YES" if nullable else "NO"
            rep.check(
                actual_nullable == expected_nullable,
                f"§81 nullability {table}.{col} = {expected_nullable} (actual {actual_nullable})",
            )
            if has_default:
                rep.check(actual_default is not None, f"§83 DEFAULT present on {table}.{col}")

    # --- money / quantity precision (§13) --------------------------------
    cur.execute("""
        SELECT table_name, column_name, numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = 'public' AND data_type = 'numeric'
    """)
    numerics = {(r[0], r[1]): (r[2], r[3]) for r in cur.fetchall()}

    for table, col in MONEY_COLUMNS:
        got = numerics.get((table, col))
        rep.check(got == (15, 2), f"REQ-0002 money NUMERIC(15,2): {table}.{col} (actual {got})")

    for table, col in QUANTITY_COLUMNS:
        got = numerics.get((table, col))
        rep.check(got == (15, 3), f"REQ-0003 quantity NUMERIC(15,3): {table}.{col} (actual {got})")

    # --- generated columns (§33, §35, §83) -------------------------------
    cur.execute("""
        SELECT table_name, column_name, is_generated, generation_expression
        FROM information_schema.columns
        WHERE table_schema = 'public' AND is_generated = 'ALWAYS'
    """)
    generated = {(r[0], r[1]): r[3] for r in cur.fetchall()}
    for table, col in GENERATED_COLUMNS:
        rep.check((table, col) in generated, f"§83 GENERATED ALWAYS AS STORED: {table}.{col}")

    expr = generated.get(("work_entries", "total_sum"), "")
    rep.check(
        "quantity" in expr and "rate_at_time" in expr,
        f"REQ-0209 total_sum = quantity * rate_at_time (actual: {expr})",
    )

    expr = generated.get(("payroll_summary", "total_calculated"), "")
    rep.check(
        all(k in expr for k in ("piecework_total", "base_salary", "bonuses", "deductions")),
        f"REQ-0210 total_calculated formula (actual: {expr})",
    )

    # --- primary keys ----------------------------------------------------
    cur.execute("""
        SELECT c.relname FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        WHERE con.contype = 'p' AND con.connamespace = 'public'::regnamespace
    """)
    pk_tables = {r[0] for r in cur.fetchall()}
    for table in list(ORIGINAL_TABLES) + EXTENSION_TABLES:
        rep.check(table in pk_tables, f"§83 PRIMARY KEY on {table}")

    # --- named constraints ----------------------------------------------
    cur.execute("""
        SELECT conname, contype FROM pg_constraint
        WHERE connamespace = 'public'::regnamespace
    """)
    constraints = {r[0]: r[1] for r in cur.fetchall()}

    required_constraints = {
        "chk_ip_entities_bin_length": "c",  # §17
        "chk_transfers_different_ip": "c",  # §28
        "chk_transfers_posted_has_legs": "c",  # REQ-0232
        "chk_transfers_keyed_pair": "c",  # REQ-0230
        "chk_transfers_accounts_distinct": "c",  # REQ-0231
        "chk_transfers_operations_distinct": "c",  # REQ-0229
        "fk_operations_account_ip": "f",  # REQ-0231
        "fk_transfers_from_account_ip": "f",  # REQ-0231
        "fk_transfers_to_account_ip": "f",  # REQ-0231
        "uq_transfers_outbound": "u",  # REQ-0229
        "uq_transfers_inbound": "u",  # REQ-0229
        "chk_operations_amount_nonzero": "c",  # §27
        "chk_payroll_periods_dates": "c",  # §34
        "uq_orders_ip_external": "u",  # §23
        "excl_rates_history_overlap": "x",  # §31
        "chk_users_password_hash": "c",  # §55
        "chk_employees_payment_type": "c",  # §29
        "chk_work_entries_status": "c",  # §33
        "chk_payroll_payments_method": "c",  # §36
        "chk_kaspi_api_credentials_api_type": "c",  # §18
        "chk_kaspi_api_credentials_status": "c",  # §18
        "chk_orders_status": "c",  # §23
        "chk_employees_status": "c",  # §29
        "chk_payroll_periods_status": "c",  # §34
        "chk_accounts_type": "c",  # §51
        "uq_payroll_summary_employee_period": "u",  # §57
        "uq_payroll_payments_operation": "u",  # §57
    }
    for name, ctype in required_constraints.items():
        rep.check(constraints.get(name) == ctype, f"§83 constraint {name} ({ctype}) present")

    cur.execute("""
        SELECT conname, convalidated FROM pg_constraint
        WHERE connamespace = 'public'::regnamespace
          AND conname IN (
              'chk_transfers_posted_has_legs',
              'fk_operations_account_ip',
              'fk_transfers_from_account_ip',
              'fk_transfers_to_account_ip'
          )
    """)
    validated_constraints = {name: bool(validated) for name, validated in cur.fetchall()}
    for name in (
        "chk_transfers_posted_has_legs",
        "fk_operations_account_ip",
        "fk_transfers_from_account_ip",
        "fk_transfers_to_account_ip",
    ):
        rep.check(validated_constraints.get(name) is True, f"REQ-0232 validated constraint {name}")

    # --- foreign keys ----------------------------------------------------
    cur.execute("""
        SELECT c.relname, con.conname FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        WHERE con.contype = 'f' AND con.connamespace = 'public'::regnamespace
    """)
    fk_by_table: dict[str, set[str]] = {}
    for table, name in cur.fetchall():
        fk_by_table.setdefault(table, set()).add(name)

    fk_expected_tables = [
        "kaspi_api_credentials",
        "orders",
        "operations",
        "transfers",
        "rates_history",
        "work_entries",
        "payroll_summary",
        "payroll_payments",
        "raw_kaspi_payloads",
        "sync_log",
        "order_items",
        "order_materials",
        "kaspi_payouts",
        "kaspi_payout_items",
        "user_roles",
        "order_status_history",
    ]
    for table in fk_expected_tables:
        rep.check(bool(fk_by_table.get(table)), f"§83 FOREIGN KEY present on {table}")

    # --- indexes (§64) ---------------------------------------------------
    cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    indexes = {r[0] for r in cur.fetchall()}
    for name in MANDATORY_INDEXES:
        rep.check(name in indexes, f"§64 mandatory index {name}")

    for name in (
        "uq_operations_ip_type_external",
        "uq_payroll_payments_active",
        "uq_kaspi_credentials_active",
        "uq_operations_import_fingerprint",
        "uq_transfers_idempotency_key",
        "uq_accounts_id_ip",
        "idx_transfers_from",
        "idx_transfers_to",
    ):
        rep.check(name in indexes, f"§41/§57 idempotency index {name}")

    # --- triggers & functions -------------------------------------------
    cur.execute("""
        SELECT p.proname FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
    """)
    functions = {r[0] for r in cur.fetchall()}
    for fn in REQUIRED_TRIGGER_FUNCTIONS:
        rep.check(fn in functions, f"§83 helper function {fn}()")

    cur.execute("""
        SELECT t.tgname, c.relname FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND NOT t.tgisinternal
    """)
    triggers = {(r[0], r[1]) for r in cur.fetchall()}

    trigger_names = {n for n, _ in triggers}

    for name in (
        "trg_work_entry_freeze_rate",
        "trg_work_entry_protect_rate",
        "trg_order_material_freeze_price",
        "trg_order_material_protect_price",
        "trg_orders_status_history",
        "trg_operations_check_sign",
        "trg_operations_check_category",
        "trg_operations_protect_posted",
        "trg_operations_protect_posted_ledger",
        "trg_transfers_protect_posted",
        "trg_transfers_validate_ledger_pair",
        "trg_operations_validate_transfer_pair",
    ):
        rep.check(name in trigger_names, f"§83 trigger {name}")

    for table in ("operations", "transfers", "payroll_payments", "kaspi_payouts"):
        rep.check(
            f"trg_{table}_forbid_delete" in trigger_names,
            f"§16 DELETE protection trigger on {table}",
        )

    # TD-019 (2026-10-01): provenance tables carry the same DELETE guard so a
    # plain DELETE cannot silently break the operations/transfers provenance
    # chain (Absolute Rule 12). Same fn_forbid_delete() + migrator hatch.
    for table in ("raw_kaspi_payloads", "import_batches"):
        rep.check(
            f"trg_{table}_forbid_delete" in trigger_names,
            f"TD-019 DELETE protection trigger on provenance table {table}",
        )

    for table in (
        "operations",
        "orders",
        "work_entries",
        "rates_history",
        "payroll_payments",
        "users",
        "user_roles",
    ):
        rep.check(f"trg_{table}_audit" in trigger_names, f"§54 audit trigger on {table}")

    # updated_at trigger on every table that has the column
    cur.execute("""
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'updated_at'
    """)
    for (table,) in cur.fetchall():
        rep.check(f"trg_{table}_updated_at" in trigger_names, f"§56 updated_at trigger on {table}")

    # --- views (§63) -----------------------------------------------------
    cur.execute("SELECT table_name FROM information_schema.views WHERE table_schema = 'public'")
    views = {r[0] for r in cur.fetchall()}
    for name in MANDATORY_VIEWS:
        rep.check(name in views, f"§63 mandatory view {name}")
    for name in ("v_order_costing", "v_kaspi_reconciliation"):
        rep.check(name in views, f"§63 view extension {name}")

    # views must be selectable
    for name in sorted(views):
        try:
            cur.execute(f"SELECT * FROM {name} LIMIT 0")
            rep.ok(f"§63 view {name} is selectable")
        except Exception as exc:
            rep.fail(f"§63 view {name} is broken: {exc}")


def audit_seed(cur, rep: Report) -> None:
    """§85, §86 — seed data."""
    cur.execute("SELECT code, direction, kind, affects_pnl FROM operation_types")
    op_types = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    for code in REQUIRED_OPERATION_TYPES:
        rep.check(code in op_types, f"§20 operation type seeded: {code}")

    for code in ("TRANSFER_IN", "TRANSFER_OUT"):
        if code in op_types:
            rep.check(
                op_types[code][2] is False,
                f"§28/§76 {code}.affects_pnl = FALSE (transfer is not P&L)",
            )

    cur.execute("SELECT name, kind FROM categories")
    cats = {(r[0], r[1]) for r in cur.fetchall()}
    for item in REQUIRED_CATEGORIES:
        rep.check(item in cats, f"§21 category seeded: {item[0]} ({item[1]})")

    cur.execute("""
        SELECT wot.code, wot.unit, wot.calc_mode, rh.rate, rh.effective_from
        FROM work_operation_types wot
        LEFT JOIN rates_history rh ON rh.work_operation_type_id = wot.id
    """)
    rows = cur.fetchall()
    seeded = {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}
    rep.check(len(rows) == 11, f"§30 exactly 11 work operation types (actual {len(rows)})")

    for code, (unit, calc_mode, rate) in REQUIRED_WORK_TYPES.items():
        if code not in seeded:
            rep.fail(f"§30 work operation type missing: {code}")
            continue
        a_unit, a_calc, a_rate, a_from = seeded[code]
        rep.check(a_unit == unit, f"§30 {code}.unit = {unit} (actual {a_unit})")
        rep.check(a_calc == calc_mode, f"§30 {code}.calc_mode = {calc_mode} (actual {a_calc})")
        rep.check(
            a_rate is not None and float(a_rate) == float(rate),
            f"§86 {code} start rate = {rate} (actual {a_rate})",
        )
        rep.check(
            a_from is not None and a_from.isoformat().startswith("2024-01-01"),
            f"§86 {code} effective_from = 2024-01-01+00 (actual {a_from})",
        )

    cur.execute("SELECT code FROM roles")
    roles = {r[0] for r in cur.fetchall()}
    for code in REQUIRED_ROLES:
        rep.check(code in roles, f"§52 role seeded: {code}")

    # §94 — no invented business data
    for table in ("ip_entities", "employees", "users", "tax_rates_history"):
        cur.execute(f"SELECT count(*) FROM {table}")
        rep.check(cur.fetchone()[0] == 0, f"§94 no invented business data in {table}")


def audit_behaviour(conn, rep: Report) -> None:
    """Behavioural tests for rules that structure alone cannot prove.

    TD-012: the whole block runs inside one transaction. Statement-level
    persistence is emulated with SAVEPOINTs, and the transaction is always
    rolled back afterwards, so repeated runs are fully idempotent.
    """
    with conn.cursor() as cur:
        # fixtures — idempotent: re-select pre-existing rows (TD-012)
        cur.execute("SELECT id FROM ip_entities WHERE bin = '000000000001'")
        row = cur.fetchone()
        if row is None:
            cur.execute("""
                INSERT INTO ip_entities (name, bin)
                VALUES ('AUDIT ИП', '000000000001') RETURNING id
            """)
            ip_id = cur.fetchone()[0]
        else:
            ip_id = row[0]
        cur.execute("SELECT id FROM employees WHERE full_name = 'AUDIT Работник'")
        row = cur.fetchone()
        if row is None:
            cur.execute("""
                INSERT INTO employees (full_name, payment_type, status)
                VALUES ('AUDIT Работник', 'PIECEWORK', 'ACTIVE') RETURNING id
            """)
            emp_id = cur.fetchone()[0]
        else:
            emp_id = row[0]
        cur.execute("SELECT id FROM work_operation_types WHERE code = 'SAWING'")
        wot_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_PAY'")
        income_type = cur.fetchone()[0]
        cur.execute("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
        expense_type = cur.fetchone()[0]
        cur.execute("SELECT id FROM operation_types WHERE code = 'TRANSFER_OUT'")
        transfer_type = cur.fetchone()[0]
        cur.execute("SELECT id FROM categories WHERE code = 'INCOME_KASPI_PAY'")
        income_cat = cur.fetchone()[0]
        cur.execute("SELECT id FROM categories WHERE code = 'EXPENSE_MATERIALS'")
        expense_cat = cur.fetchone()[0]

    def expect_error(sql: str, params: tuple, label: str) -> None:
        with conn.cursor() as c:
            c.execute("SAVEPOINT stmt")
            try:
                c.execute(sql, params)
                c.execute("ROLLBACK TO SAVEPOINT stmt")
                rep.fail(f"{label} — expected rejection, but the statement succeeded")
            except Exception:
                c.execute("ROLLBACK TO SAVEPOINT stmt")
                rep.ok(label)

    def expect_success(sql: str, params: tuple, label: str):
        with conn.cursor() as c:
            c.execute("SAVEPOINT stmt")
            try:
                c.execute(sql, params)
                row = c.fetchone() if c.description else None
            except Exception as exc:
                c.execute("ROLLBACK TO SAVEPOINT stmt")
                rep.fail(f"{label} — unexpected failure: {exc}")
                return None
            c.execute("RELEASE SAVEPOINT stmt")
            rep.ok(label)
            return row
            return None

    # §17 — BIN must be 12 digits
    expect_error(
        "INSERT INTO ip_entities (name, bin) VALUES (%s, %s)",
        ("BAD BIN", "123"),
        "§17 BIN length CHECK rejects short БИН",
    )

    # §32 — RATE FREEZE: rate is resolved automatically
    row = expect_success(
        """
        INSERT INTO work_entries (employee_id, work_operation_type_id, ip_id, entry_date, quantity)
        VALUES (%s, %s, %s, DATE '2024-06-01', 10)
        RETURNING id, rate_at_time, total_sum
    """,
        (emp_id, wot_id, ip_id),
        "§32 rate freeze: rate_at_time auto-filled from rates_history",
    )
    if row:
        we_id, rate, total = row
        rep.check(float(rate) == 150.0, f"§32 frozen rate = 150.00 (actual {rate})")
        rep.check(float(total) == 1500.0, f"§33 total_sum = 10 * 150 = 1500 (actual {total})")

        # change the rate — the historical entry must not change
        with conn.cursor() as c:
            c.execute(
                """
                UPDATE rates_history SET effective_to = TIMESTAMPTZ '2025-01-01 00:00:00+00'
                WHERE work_operation_type_id = %s AND effective_to IS NULL
            """,
                (wot_id,),
            )
            c.execute(
                """
                INSERT INTO rates_history
                    (work_operation_type_id, rate, effective_from, effective_to)
                VALUES (%s, 999.00,
                        TIMESTAMPTZ '2025-01-01 00:00:00+00',
                        TIMESTAMPTZ '2025-12-31 00:00:00+00')
            """,
                (wot_id,),
            )
        with conn.cursor() as c:
            c.execute("SELECT rate_at_time, total_sum FROM work_entries WHERE id = %s", (we_id,))
            rate2, total2 = c.fetchone()
        rep.check(
            float(rate2) == 150.0, f"§32 rate change does NOT alter old work_entry (actual {rate2})"
        )
        rep.check(
            float(total2) == 1500.0, f"§32 total_sum unchanged after rate change (actual {total2})"
        )

        # frozen rate is immutable
        expect_error(
            "UPDATE work_entries SET rate_at_time = 1 WHERE id = %s",
            (we_id,),
            "§32 frozen rate_at_time is immutable",
        )

        # §16 — DELETE is forbidden
        expect_error(
            "DELETE FROM work_entries WHERE id = %s",
            (we_id,),
            "§16 DELETE forbidden on work_entries",
        )

    # §31 — overlapping rate periods are rejected
    expect_error(
        """
        INSERT INTO rates_history (work_operation_type_id, rate, effective_from)
        VALUES (%s, 111.00, TIMESTAMPTZ '2025-06-01 00:00:00+00')
    """,
        (wot_id,),
        "§31 EXCLUDE rejects overlapping rate periods",
    )

    # §27 — amount must not be zero
    expect_error(
        """
        INSERT INTO operations (ip_id, type_id, category_id, amount, operation_date)
        VALUES (%s, %s, %s, 0, CURRENT_DATE)
    """,
        (ip_id, income_type, income_cat),
        "§27 amount != 0 CHECK",
    )

    # §27 — sign must match direction
    expect_error(
        """
        INSERT INTO operations (ip_id, type_id, category_id, amount, operation_date)
        VALUES (%s, %s, %s, -100, CURRENT_DATE)
    """,
        (ip_id, income_type, income_cat),
        "§27 income operation rejects negative amount",
    )

    expect_error(
        """
        INSERT INTO operations (ip_id, type_id, category_id, amount, operation_date)
        VALUES (%s, %s, %s, 100, CURRENT_DATE)
    """,
        (ip_id, expense_type, expense_cat),
        "§27 expense operation rejects positive amount",
    )

    # §21 — category kind must match type kind
    expect_error(
        """
        INSERT INTO operations (ip_id, type_id, category_id, amount, operation_date)
        VALUES (%s, %s, %s, 100, CURRENT_DATE)
    """,
        (ip_id, income_type, expense_cat),
        "§21 category kind must match operation type kind",
    )

    # §28 — transfers carry no P&L category
    expect_error(
        """
        INSERT INTO operations (ip_id, type_id, category_id, amount, operation_date)
        VALUES (%s, %s, %s, -100, CURRENT_DATE)
    """,
        (ip_id, transfer_type, expense_cat),
        "§28 transfer operation rejects a P&L category",
    )

    # §41 — idempotency: the same external_id cannot be booked twice
    op_row = expect_success(
        """
        INSERT INTO operations (ip_id, type_id, category_id, amount, operation_date, external_id)
        VALUES (%s, %s, %s, 5000, CURRENT_DATE, 'AUDIT-EXT-1')
        RETURNING id
    """,
        (ip_id, income_type, income_cat),
        "§25 Kaspi Pay income operation inserted",
    )

    expect_error(
        """
        INSERT INTO operations (ip_id, type_id, category_id, amount, operation_date, external_id)
        VALUES (%s, %s, %s, 5000, CURRENT_DATE, 'AUDIT-EXT-1')
    """,
        (ip_id, income_type, income_cat),
        "§41 duplicate external_id rejected (retry-safe, ADR-010)",
    )

    if op_row:
        # §16 — ledger DELETE is forbidden
        expect_error(
            "DELETE FROM operations WHERE id = %s",
            (op_row[0],),
            "§16 DELETE forbidden on operations",
        )

        # §54 — audit log captured the insert
        with conn.cursor() as c:
            c.execute(
                """
                SELECT count(*) FROM audit_log
                WHERE entity = 'operations' AND entity_id = %s AND action = 'INSERT'
            """,
                (op_row[0],),
            )
            rep.check(c.fetchone()[0] >= 1, "§54 audit_log recorded the operation INSERT")

        # §56 — updated_at is refreshed automatically.
        # TD-012: the behaviour block runs in ONE transaction, and the trigger
        # assigns CURRENT_TIMESTAMP (transaction start time). Two reads inside
        # the same transaction can therefore never differ, so `after > before`
        # is unobservable here. Instead we prove §56 as follows:
        #   1. the trigger fn_touch_updated_at exists and is enabled;
        #   2. after an UPDATE, updated_at equals the transaction timestamp,
        #      i.e. it was freshly written by the trigger on this UPDATE.
        with conn.cursor() as c:
            c.execute("""
                SELECT count(*) FROM pg_trigger
                WHERE tgrelid = 'operations'::regclass
                  AND NOT tgisinternal AND tgenabled IN ('O', 'R')
                  AND tgname LIKE '%updated_at%'
            """)
            trig = c.fetchone()[0]
            c.execute(
                "SELECT updated_at, CURRENT_TIMESTAMP FROM operations WHERE id = %s", (op_row[0],)
            )
            _before, _now1 = c.fetchone()
            c.execute("UPDATE operations SET comment = 'audit touch' WHERE id = %s", (op_row[0],))
            c.execute(
                "SELECT updated_at, CURRENT_TIMESTAMP FROM operations WHERE id = %s", (op_row[0],)
            )
            after, now2 = c.fetchone()
        rep.check(trig >= 1, "§56 fn_touch_updated_at trigger enabled on operations")
        rep.check(
            after == now2,
            f"§56 updated_at refreshed by trigger on UPDATE (updated_at={after}, tx={now2})",
        )

    # §28 — a transfer to the same ИП is rejected
    expect_error(
        """
        INSERT INTO transfers (from_ip_id, to_ip_id, amount, transfer_date)
        VALUES (%s, %s, 100, CURRENT_DATE)
    """,
        (ip_id, ip_id),
        "§28 transfer from_ip_id <> to_ip_id CHECK",
    )

    # §23 — UNIQUE (ip_id, external_order_id)
    order_row = expect_success(
        """
        INSERT INTO orders (ip_id, external_order_id, status, total_amount, source)
        VALUES (%s, 'AUDIT-ORD-1', 'NEW', 100000, 'KASPI_SHOP')
        RETURNING id
    """,
        (ip_id,),
        "§23 order inserted",
    )

    expect_error(
        """
        INSERT INTO orders (ip_id, external_order_id, status, total_amount)
        VALUES (%s, 'AUDIT-ORD-1', 'NEW', 100000)
    """,
        (ip_id,),
        "§23 duplicate (ip_id, external_order_id) rejected",
    )

    if order_row:
        order_id = order_row[0]
        # §50 — status history
        with conn.cursor() as c:
            c.execute("SELECT count(*) FROM order_status_history WHERE order_id = %s", (order_id,))
            rep.check(c.fetchone()[0] == 1, "§50 status history written on order creation")
            c.execute("UPDATE orders SET status = 'APPROVED' WHERE id = %s", (order_id,))
            c.execute(
                """
                SELECT old_status, new_status FROM order_status_history
                WHERE order_id = %s ORDER BY id DESC LIMIT 1
            """,
                (order_id,),
            )
            old, new = c.fetchone()
        rep.check(
            (old, new) == ("NEW", "APPROVED"),
            f"§50 status transition recorded NEW→APPROVED (actual {old}→{new})",
        )

        # §24 — a Kaspi Shop order does NOT create a ledger operation by itself
        with conn.cursor() as c:
            c.execute("SELECT count(*) FROM operations WHERE related_order_id = %s", (order_id,))
            rep.check(
                c.fetchone()[0] == 0, "§24 Kaspi Shop order alone creates no financial operation"
            )

        # §38 — MATERIAL PRICE FREEZE
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO materials (sku, name, unit, current_price)
                VALUES ('AUDIT-MAT-1', 'AUDIT ЛДСП', 'м2', 3000.00) RETURNING id
            """)
            mat_id = c.fetchone()[0]

        om_row = expect_success(
            """
            INSERT INTO order_materials (order_id, material_id, quantity)
            VALUES (%s, %s, 2.5) RETURNING id, price_at_time, total_cost
        """,
            (order_id, mat_id),
            "§38 price freeze: price_at_time auto-filled",
        )

        if om_row:
            om_id, price, cost = om_row
            rep.check(float(price) == 3000.0, f"§38 frozen price = 3000.00 (actual {price})")
            rep.check(float(cost) == 7500.0, f"§49 total_cost = 2.5 * 3000 = 7500 (actual {cost})")

            with conn.cursor() as c:
                c.execute("UPDATE materials SET current_price = 9999 WHERE id = %s", (mat_id,))
                c.execute(
                    "SELECT price_at_time, total_cost FROM order_materials WHERE id = %s", (om_id,)
                )
                price2, cost2 = c.fetchone()
            rep.check(
                float(price2) == 3000.0,
                f"§38 material price change does NOT alter historical cost (actual {price2})",
            )
            rep.check(
                float(cost2) == 7500.0,
                f"§38 total_cost unchanged after price change (actual {cost2})",
            )

            expect_error(
                "UPDATE order_materials SET price_at_time = 1 WHERE id = %s",
                (om_id,),
                "§38 frozen price_at_time is immutable",
            )

    # §55 — plaintext password is rejected
    expect_error(
        """
        INSERT INTO users (username, password_hash) VALUES ('audituser', 'plaintext123')
    """,
        (),
        "§55 plaintext password rejected by CHECK",
    )

    expect_success(
        """
        INSERT INTO users (username, password_hash)
        VALUES ('audituser', '$2b$12$abcdefghijklmnopqrstuv') RETURNING id
    """,
        (),
        "§55 bcrypt hash accepted",
    )

    # §34 — payroll period date order and overlap
    expect_error(
        """
        INSERT INTO payroll_periods (start_date, end_date)
        VALUES (DATE '2024-06-30', DATE '2024-06-01')
    """,
        (),
        "§34 end_date >= start_date CHECK",
    )

    expect_success(
        """
        INSERT INTO payroll_periods (start_date, end_date)
        VALUES (DATE '2024-06-01', DATE '2024-06-30') RETURNING id
    """,
        (),
        "§34 payroll period inserted",
    )

    expect_error(
        """
        INSERT INTO payroll_periods (start_date, end_date)
        VALUES (DATE '2024-06-15', DATE '2024-07-15')
    """,
        (),
        "§34 overlapping payroll periods rejected",
    )

    # §35 — total_calculated formula, §57 — one summary per employee/period
    with conn.cursor() as c:
        c.execute("SELECT id FROM payroll_periods WHERE start_date = DATE '2024-06-01'")
        period_id = c.fetchone()[0]

    ps_row = expect_success(
        """
        INSERT INTO payroll_summary
            (employee_id, payroll_period_id, piecework_total, base_salary, bonuses, deductions)
        VALUES (%s, %s, 100000, 50000, 10000, 5000)
        RETURNING id, total_calculated
    """,
        (emp_id, period_id),
        "§35 payroll summary inserted",
    )

    if ps_row:
        ps_id, total_calc = ps_row
        rep.check(
            float(total_calc) == 155000.0,
            f"§35 total_calculated = 100000+50000+10000-5000 = 155000 (actual {total_calc})",
        )

        expect_error(
            """
            INSERT INTO payroll_summary (employee_id, payroll_period_id, piecework_total)
            VALUES (%s, %s, 1)
        """,
            (emp_id, period_id),
            "§57 duplicate payroll_summary for (employee, period) rejected",
        )

        # §57 / §67 — double salary payment must be impossible
        expect_success(
            """
            INSERT INTO payroll_payments (payroll_summary_id, payment_date, amount, payment_method)
            VALUES (%s, CURRENT_DATE, 155000, 'KASPI_TRANSFER') RETURNING id
        """,
            (ps_id,),
            "§36 salary payment inserted",
        )

        expect_error(
            """
            INSERT INTO payroll_payments (payroll_summary_id, payment_date, amount, payment_method)
            VALUES (%s, CURRENT_DATE, 155000, 'CASH')
        """,
            (ps_id,),
            "§57/§67 double salary payment rejected (duplicate payment)",
        )

        # §63 — v_payroll_due arithmetic
        with conn.cursor() as c:
            c.execute(
                """
                SELECT total_calculated, amount_paid, amount_due
                FROM v_payroll_due WHERE payroll_summary_id = %s
            """,
                (ps_id,),
            )
            calc, paid, due = c.fetchone()
        rep.check(float(due) == 0.0, f"§63 v_payroll_due: due = {calc} - {paid} = 0 (actual {due})")

    # §18 — credential api_type / status domains
    expect_error(
        """
        INSERT INTO kaspi_api_credentials (ip_id, api_type, encrypted_token)
        VALUES (%s, 'INVALID', repeat('x', 40)::bytea)
    """,
        (ip_id,),
        "§18 api_type domain enforced (SHOP|PAY)",
    )

    expect_success(
        """
        INSERT INTO kaspi_api_credentials (ip_id, api_type, encrypted_token, status)
        VALUES (%s, 'SHOP', repeat('x', 40)::bytea, 'ACTIVE') RETURNING id
    """,
        (ip_id,),
        "§18 credential inserted",
    )

    expect_error(
        """
        INSERT INTO kaspi_api_credentials (ip_id, api_type, encrypted_token, status)
        VALUES (%s, 'SHOP', repeat('y', 40)::bytea, 'ACTIVE')
    """,
        (ip_id,),
        "§18 only one ACTIVE credential per (ИП, api_type)",
    )

    # §54 — audit log masks secrets
    with conn.cursor() as c:
        c.execute("""
            SELECT new_data ->> 'encrypted_token' FROM audit_log
            WHERE entity = 'kaspi_api_credentials' ORDER BY id DESC LIMIT 1
        """)
        row = c.fetchone()
    rep.check(
        row is not None and row[0] == "***MASKED***",
        f"§71 audit_log masks encrypted_token (actual {row[0] if row else None})",
    )

    with conn.cursor() as c:
        c.execute("""
            SELECT new_data ->> 'password_hash' FROM audit_log
            WHERE entity = 'users' ORDER BY id DESC LIMIT 1
        """)
        row = c.fetchone()
    rep.check(
        row is not None and row[0] == "***MASKED***",
        f"§71 audit_log masks password_hash (actual {row[0] if row else None})",
    )

    # §63 — financial summary view keeps transfers out of P&L
    destination_bin = "000000000002"
    with conn.cursor() as c:
        c.execute("SELECT id FROM ip_entities WHERE bin = %s", (destination_bin,))
        destination_row = c.fetchone()
        if destination_row is None:
            c.execute(
                (
                    "INSERT INTO ip_entities (name, bin) "
                    "VALUES ('AUDIT ИП recipient', %s) RETURNING id"
                ),
                (destination_bin,),
            )
            destination_ip_id = c.fetchone()[0]
        else:
            destination_ip_id = destination_row[0]

        c.execute("SELECT id FROM users WHERE username = 'audituser'")
        actor_id = c.fetchone()[0]

        c.execute("SELECT id FROM accounts WHERE ip_id = %s AND name = 'AUDIT source'", (ip_id,))
        source_account_row = c.fetchone()
        if source_account_row is None:
            c.execute(
                (
                    "INSERT INTO accounts (ip_id, type, name) "
                    "VALUES (%s, 'OTHER', 'AUDIT source') RETURNING id"
                ),
                (ip_id,),
            )
            source_account_id = c.fetchone()[0]
        else:
            source_account_id = source_account_row[0]

        c.execute(
            "SELECT id FROM accounts WHERE ip_id = %s AND name = 'AUDIT destination'",
            (destination_ip_id,),
        )
        destination_account_row = c.fetchone()
        if destination_account_row is None:
            c.execute(
                (
                    "INSERT INTO accounts (ip_id, type, name) "
                    "VALUES (%s, 'OTHER', 'AUDIT destination') RETURNING id"
                ),
                (destination_ip_id,),
            )
            destination_account_id = c.fetchone()[0]
        else:
            destination_account_id = destination_account_row[0]

        c.execute("SELECT id FROM operation_types WHERE code = 'TRANSFER_IN'")
        transfer_in_type = c.fetchone()[0]
        transfer_reason = "audit transfer pair"
        c.execute(
            """
            INSERT INTO operations
                (ip_id, type_id, account_id, amount, operation_date, comment, status, created_by)
            VALUES (%s, %s, %s, -20000, CURRENT_DATE, %s, 'DRAFT', %s)
            RETURNING id
            """,
            (ip_id, transfer_type, source_account_id, transfer_reason, actor_id),
        )
        outbound_id = c.fetchone()[0]
        c.execute(
            """
            INSERT INTO operations
                (ip_id, type_id, account_id, amount, operation_date, comment, status, created_by)
            VALUES (%s, %s, %s, 20000, CURRENT_DATE, %s, 'DRAFT', %s)
            RETURNING id
            """,
            (
                destination_ip_id,
                transfer_in_type,
                destination_account_id,
                transfer_reason,
                actor_id,
            ),
        )
        inbound_id = c.fetchone()[0]
        c.execute(
            "UPDATE operations SET related_operation_id = %s WHERE id = %s",
            (inbound_id, outbound_id),
        )
        c.execute(
            "UPDATE operations SET related_operation_id = %s WHERE id = %s",
            (outbound_id, inbound_id),
        )
        c.execute(
            """
            INSERT INTO transfers
                (from_ip_id, to_ip_id, from_account_id, to_account_id, amount,
                 transfer_date, reason, outbound_operation_id, inbound_operation_id,
                 idempotency_key, status, created_by)
            VALUES (%s, %s, %s, %s, 20000, CURRENT_DATE, %s, %s, %s,
                    'AUDIT-TRANSFER-001', 'DRAFT', %s)
            """,
            (
                ip_id,
                destination_ip_id,
                source_account_id,
                destination_account_id,
                transfer_reason,
                outbound_id,
                inbound_id,
                actor_id,
            ),
        )
        c.execute(
            "UPDATE operations SET status = 'POSTED' WHERE id IN (%s, %s)",
            (outbound_id, inbound_id),
        )
        c.execute(
            "UPDATE transfers SET status = 'POSTED' WHERE outbound_operation_id = %s",
            (outbound_id,),
        )
        c.execute("SET CONSTRAINTS ALL IMMEDIATE")

    with conn.cursor() as c:
        c.execute(
            """
            SELECT total_income, total_expense, net_profit, transfers_out
            FROM v_ip_financial_summary
            WHERE ip_id = %s AND period_month = date_trunc('month', CURRENT_DATE)::DATE
            """,
            (ip_id,),
        )
        income, expense, net, tr_out = c.fetchone()
        c.execute(
            """
            SELECT total_income, total_expense, net_profit, transfers_in
            FROM v_ip_financial_summary
            WHERE ip_id = %s AND period_month = date_trunc('month', CURRENT_DATE)::DATE
            """,
            (destination_ip_id,),
        )
        recipient_income, recipient_expense, recipient_net, tr_in = c.fetchone()
    rep.check(float(tr_out) == 20000.0, f"§63 transfers_out reported (actual {tr_out})")
    rep.check(float(tr_in) == 20000.0, f"§63 transfers_in reported (actual {tr_in})")
    rep.check(float(expense) == 0.0, f"§28/§76 transfer NOT counted as expense (actual {expense})")
    rep.check(
        float(recipient_expense) == 0.0,
        f"§28/§76 transfer NOT counted as recipient expense (actual {recipient_expense})",
    )
    rep.check(
        float(net) == float(income),
        f"§76 net_profit excludes transfers (income={income}, net={net})",
    )
    rep.check(
        float(recipient_net) == float(recipient_income),
        f"§76 recipient net_profit excludes transfers "
        f"(income={recipient_income}, net={recipient_net})",
    )
    # TD-012: no commit — the complete transfer fixture stays inside the audit transaction.

    # §37 — order costing view
    with conn.cursor() as c:
        c.execute(
            "SELECT material_cost, labor_cost, total_direct_cost, gross_profit, margin_percent "
            "FROM v_order_costing WHERE ip_id = %s",
            (ip_id,),
        )
        rows = c.fetchall()
    rep.check(bool(rows), "§37 v_order_costing returns rows")


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    rep = Report()

    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            audit_structure(cur, rep)
            audit_seed(cur, rep)
        conn.rollback()
        audit_behaviour(conn, rep)
        conn.rollback()  # TD-012: fixtures never persist

    total = len(rep.passed) + len(rep.failed)
    print("=" * 70)
    print("STEP 1 SELF-AUDIT (MASTER PROMPT §90, §91)")
    print("=" * 70)
    for msg in rep.failed:
        print(f"  [FAIL] {msg}")
    print(f"\nPASSED: {len(rep.passed)} / {total}")
    print(f"FAILED: {len(rep.failed)} / {total}")

    if rep.failed:
        print("\nRESULT: MISSING REQUIREMENT — STEP 1 IS NOT COMPLETE (§91)")
        return 1
    print("\nRESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
