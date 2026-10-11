"""
Conformance test: SQLAlchemy models vs the applied PostgreSQL schema.

MASTER PROMPT §91 forbids declaring a step complete while a MISSING
REQUIREMENT exists. This test is the executable form of that rule for DAY 2 of
PLAN v1.0 (REQ-0318): it proves that app/models is a faithful 1:1 mapping of
database/schema.sql as actually applied to PostgreSQL, rather than a
plausible-looking approximation.

What is verified
----------------
1. Table set               — exactly the 31 tables of §80–§82, no extras.
2. §80 / §81               — the 17 original tables exist and every original
                             column is still present.
3. Column set              — model columns == database columns, per table.
4. Physical types          — NUMERIC(15,2) money (§13), NUMERIC(15,3)
                             quantity (§14), TIMESTAMPTZ (§15), CHAR(64)
                             fingerprints, BYTEA token, JSONB payloads.
5. Nullability             — NOT NULL matches on every column.
6. Server defaults         — a model must not claim a default the database
                             does not have, and vice versa.
7. GENERATED columns (§33, §35, §47, §49) — all four are declared as
                             Computed(persisted=True) in the models.
8. Primary keys            — including the composite PK of user_roles.
9. Foreign keys            — target table, target column and ON DELETE action.
10. UNIQUE constraints     — table-level uniqueness matches.
11. CHECK constraint names — every database CHECK is declared in the model.
12. EXCLUDE constraints (§31, §34, §75) — all three are declared.
13. Indexes                — every database index is declared in the model,
                             including the §64 mandatory four and the partial
                             unique idempotency indexes of §41 / ADR-010.
14. Views (§63)            — the five views exist and are NOT mapped as models.
15. ORM mappers            — configure_mappers() succeeds (relationships are
                             consistent and unambiguous).

The database is the source of truth (ADR-011): when this test fails, the model
is wrong unless database/schema.sql was deliberately changed first.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CheckConstraint, Index, Table, UniqueConstraint
from sqlalchemy.sql.schema import Computed

from app.models import (
    EXTENSION_TABLE_REQUIREMENTS,
    ORIGINAL_TABLE_NAMES,
    VIEW_NAMES,
    Base,
)

pytestmark = pytest.mark.usefixtures("applied_schema")

METADATA = Base.metadata

# ---------------------------------------------------------------------------
# §80 — the original columns that must never disappear (§81).
#
# Only the columns listed by the MASTER PROMPT are enumerated here: later
# production additions are allowed (§81) and are covered by the generic
# column-set comparison against the database.
# ---------------------------------------------------------------------------
ORIGINAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "ip_entities": ("id", "name", "bin", "is_active", "created_at", "closed_at"),
    "kaspi_api_credentials": (
        "id",
        "ip_id",
        "api_type",
        "encrypted_token",
        "expires_at",
        "status",
        "created_at",
        "updated_at",
    ),
    "operation_types": (
        "id",
        "code",
        "name",
        "direction",
        "kind",
        "affects_pnl",
        "is_system",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "categories": (
        "id",
        "code",
        "name",
        "kind",
        "parent_id",
        "is_system",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "counterparties": ("id", "name", "bin_iin", "phone", "comment", "created_at"),
    "orders": (
        "id",
        "ip_id",
        "external_order_id",
        "customer_name",
        "customer_phone",
        "delivery_address",
        "status",
        "total_amount",
        "kaspi_commission",
        "delivery_fee",
        "created_at",
        "updated_at",
    ),
    "raw_kaspi_payloads": ("id", "ip_id", "request_type", "received_at", "payload"),
    "operations": (
        "id",
        "ip_id",
        "type_id",
        "category_id",
        "counterparty_id",
        "amount",
        "operation_date",
        "external_id",
        "related_order_id",
        "related_operation_id",
        "raw_payload_id",
        "comment",
        "created_at",
    ),
    "transfers": (
        "id",
        "from_ip_id",
        "to_ip_id",
        "amount",
        "transfer_date",
        "reason",
        "outbound_operation_id",
        "inbound_operation_id",
    ),
    "sync_log": (
        "id",
        "ip_id",
        "api_type",
        "sync_started_at",
        "sync_finished_at",
        "records_fetched",
        "records_inserted",
        "records_updated",
        "errors_count",
        "error_details",
    ),
    "employees": (
        "id",
        "full_name",
        "phone",
        "position",
        "assigned_ip_id",
        "hired_at",
        "fired_at",
        "status",
        "payment_type",
    ),
    "work_operation_types": (
        "id",
        "code",
        "name",
        "unit",
        "calc_mode",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "rates_history": (
        "id",
        "work_operation_type_id",
        "rate",
        "effective_from",
        "effective_to",
    ),
    "work_entries": (
        "id",
        "employee_id",
        "work_operation_type_id",
        "order_id",
        "ip_id",
        "entry_date",
        "quantity",
        "rate_at_time",
        "total_sum",
        "created_by_user",
        "status",
        "created_at",
    ),
    "payroll_periods": ("id", "start_date", "end_date", "status"),
    "payroll_summary": (
        "id",
        "employee_id",
        "payroll_period_id",
        "piecework_total",
        "base_salary",
        "bonuses",
        "deductions",
        "total_calculated",
        "created_at",
    ),
    "payroll_payments": (
        "id",
        "payroll_summary_id",
        "payment_date",
        "amount",
        "payment_method",
        "operation_id",
    ),
}

#: §64 — index names that must exist verbatim.
MANDATORY_INDEXES: dict[str, str] = {
    "idx_operations_analytics": "operations",
    "idx_work_entries_emp": "work_entries",
    "idx_orders_status": "orders",
    "idx_rates_lookup": "rates_history",
}

#: §33, §35, §47, §49 — the four GENERATED ALWAYS ... STORED columns.
GENERATED_COLUMNS: dict[tuple[str, str], str] = {
    ("work_entries", "total_sum"): "quantity * rate_at_time",
    ("payroll_summary", "total_calculated"): (
        "piecework_total + base_salary + bonuses - deductions"
    ),
    ("order_items", "total_amount"): "round(quantity * unit_price, 2) - discount",
    ("order_materials", "total_cost"): "round(quantity * price_at_time, 2)",
}

#: §31, §34, §75 — EXCLUDE constraints and their table.
EXCLUDE_CONSTRAINTS: dict[str, str] = {
    "excl_payroll_periods_overlap": "payroll_periods",
    "excl_rates_history_overlap": "rates_history",
    "excl_tax_rates_history_overlap": "tax_rates_history",
}

#: §41 / ADR-010 — partial unique indexes that provide race-safe idempotency.
IDEMPOTENCY_INDEXES: dict[str, str] = {
    "uq_operations_ip_type_external": "operations",
    "uq_operations_import_fingerprint": "operations",
    "uq_work_entries_import_fingerprint": "work_entries",
    "uq_raw_payloads_hash": "raw_kaspi_payloads",
    "uq_kaspi_credentials_active": "kaspi_api_credentials",
    "uq_kaspi_payout_items_order": "kaspi_payout_items",
    "uq_payroll_payments_active": "payroll_payments",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalise_default(value: str | None) -> str | None:
    """
    Reduce a server default to a comparable form.

    PostgreSQL reports defaults with explicit casts and sequence calls
    ("'ACTIVE'::character varying", "nextval('..._id_seq'::regclass)"), while
    the models declare them as plain SQL text. Both sides are normalised
    before comparison so that only real differences are reported.
    """
    if value is None:
        return None
    text_value = value.strip()
    if text_value.startswith("nextval("):
        return "__sequence__"
    # Drop PostgreSQL type casts: 'x'::character varying -> 'x'
    text_value = re.sub(r"::[a-zA-Z ]+(\[\])?", "", text_value)
    text_value = text_value.strip().strip("()").strip()
    lowered = text_value.lower()
    if lowered in {"current_timestamp", "now()"}:
        return "current_timestamp"
    if lowered in {"true", "false"}:
        return lowered
    return text_value


#: Dialect used to render both sides of a type comparison as real PostgreSQL
#: DDL. Comparing str(column.type) would compare Python class names
#: (DATETIME vs TIMESTAMP, BLOB vs BYTEA) instead of the physical types.
_PG_DIALECT = postgresql.dialect()


def _type_signature(type_) -> str:
    """Render a type as the PostgreSQL DDL fragment it compiles to."""
    return type_.compile(dialect=_PG_DIALECT).upper().replace(" ", "")


def _db_type_signature(column: dict) -> str:
    """Build a comparable signature of a database column type."""
    return _type_signature(column["type"])


def _model_type_signature(column) -> str:
    """Build a comparable signature of a model column type."""
    return _type_signature(column.type)


#: Tables that exist in the database but are deliberately NOT mapped.
#:
#: alembic_version is Alembic's own bookkeeping table (one column, one row).
#: Declaring an ORM model for it would let application code write to a table
#: owned by the migration tool, so it is excluded from the comparison instead.
#: It appears only after `alembic upgrade head`; a database provisioned solely
#: by scripts/apply_schema.sh does not have it, and both states must pass.
UNMAPPED_INFRASTRUCTURE_TABLES: frozenset[str] = frozenset({"alembic_version"})


@pytest.fixture(scope="module")
def db_tables(engine: Engine) -> dict[str, dict]:
    """Reflect the applied schema once for the whole module."""
    inspector = inspect(engine)
    reflected: dict[str, dict] = {}
    for table_name in inspector.get_table_names():
        if table_name in UNMAPPED_INFRASTRUCTURE_TABLES:
            continue
        reflected[table_name] = {
            "columns": {c["name"]: c for c in inspector.get_columns(table_name)},
            "pk": inspector.get_pk_constraint(table_name),
            "fks": inspector.get_foreign_keys(table_name),
            "uniques": inspector.get_unique_constraints(table_name),
            "checks": inspector.get_check_constraints(table_name),
            "indexes": inspector.get_indexes(table_name),
        }
    return reflected


# ---------------------------------------------------------------------------
# 1. Table set
# ---------------------------------------------------------------------------
def test_orm_mappers_configure() -> None:
    """Every relationship resolves; no ambiguous joins (item 15)."""
    configure_mappers()


def test_table_set_matches_database(db_tables: dict[str, dict]) -> None:
    """The models declare exactly the tables that exist in the database."""
    model_tables = set(METADATA.tables)
    database_tables = set(db_tables)
    assert model_tables == database_tables, (
        f"missing in models: {sorted(database_tables - model_tables)}; "
        f"unknown to database: {sorted(model_tables - database_tables)}"
    )


def test_table_count_is_31() -> None:
    """§80 (17 original) + §82 (17 extensions) = 34 tables + TD-015 store."""
    assert len(ORIGINAL_TABLE_NAMES) == 17
    assert len(EXTENSION_TABLE_REQUIREMENTS) == 20  # 17 + stock (0021) + production_output (0023)
    assert len(METADATA.tables) == 39  # 34 + auth_rate_events + settlement_links + 2 stock (0021) + production_output (0023)


@pytest.mark.parametrize("table_name", ORIGINAL_TABLE_NAMES)
def test_original_table_is_mapped(table_name: str) -> None:
    """§81 — no original table may be renamed or dropped."""
    assert table_name in METADATA.tables


@pytest.mark.parametrize("table_name", sorted(EXTENSION_TABLE_REQUIREMENTS))
def test_extension_table_is_mapped(table_name: str) -> None:
    """§82 — every declared architectural extension is mapped."""
    assert table_name in METADATA.tables


@pytest.mark.parametrize("table_name,columns", sorted(ORIGINAL_COLUMNS.items()))
def test_original_columns_preserved(table_name: str, columns: tuple[str, ...]) -> None:
    """§81 — every original column is still present in the model."""
    table: Table = METADATA.tables[table_name]
    missing = [name for name in columns if name not in table.c]
    assert not missing, f"{table_name}: original columns missing from model: {missing}"


# ---------------------------------------------------------------------------
# 3–6. Columns: names, types, nullability, defaults
# ---------------------------------------------------------------------------
def test_column_sets_match(db_tables: dict[str, dict]) -> None:
    problems: list[str] = []
    for table_name, table in sorted(METADATA.tables.items()):
        if table_name not in db_tables:
            continue
        model_columns = set(table.c.keys())
        db_columns = set(db_tables[table_name]["columns"])
        if model_columns != db_columns:
            problems.append(
                f"{table_name}: missing in model {sorted(db_columns - model_columns)}, "
                f"absent in database {sorted(model_columns - db_columns)}"
            )
    assert not problems, "\n".join(problems)


def test_column_types_match(db_tables: dict[str, dict]) -> None:
    """§13, §14, §15 — physical types must be identical, not merely similar."""
    problems: list[str] = []
    for table_name, table in sorted(METADATA.tables.items()):
        if table_name not in db_tables:
            continue
        for column in table.c:
            db_column = db_tables[table_name]["columns"].get(column.name)
            if db_column is None:
                continue
            expected = _db_type_signature(db_column)
            actual = _model_type_signature(column)
            if expected != actual:
                problems.append(f"{table_name}.{column.name}: database={expected} model={actual}")
    assert not problems, "\n".join(problems)


def test_column_nullability_matches(db_tables: dict[str, dict]) -> None:
    problems: list[str] = []
    for table_name, table in sorted(METADATA.tables.items()):
        if table_name not in db_tables:
            continue
        for column in table.c:
            db_column = db_tables[table_name]["columns"].get(column.name)
            if db_column is None:
                continue
            if bool(db_column["nullable"]) != bool(column.nullable):
                problems.append(
                    f"{table_name}.{column.name}: database nullable="
                    f"{db_column['nullable']} model nullable={column.nullable}"
                )
    assert not problems, "\n".join(problems)


def test_server_defaults_match(db_tables: dict[str, dict]) -> None:
    problems: list[str] = []
    for table_name, table in sorted(METADATA.tables.items()):
        if table_name not in db_tables:
            continue
        for column in table.c:
            db_column = db_tables[table_name]["columns"].get(column.name)
            if db_column is None:
                continue
            if isinstance(column.server_default, Computed):
                continue  # covered by test_generated_columns
            db_default = _normalise_default(db_column.get("default"))
            model_default = None
            if column.server_default is not None:
                model_default = _normalise_default(
                    str(column.server_default.arg)  # type: ignore[union-attr]
                )
            # A single-column BIGSERIAL primary key is backed by a sequence;
            # a composite primary key (user_roles) is not. Only claim the
            # implicit sequence default when the database actually has one.
            if (
                column.primary_key
                and column.autoincrement
                and model_default is None
                and len(table.primary_key.columns) == 1
            ):
                model_default = "__sequence__"
            if db_default != model_default:
                problems.append(
                    f"{table_name}.{column.name}: database default={db_default!r} "
                    f"model default={model_default!r}"
                )
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# 7. GENERATED columns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", sorted(GENERATED_COLUMNS))
def test_generated_columns(key: tuple[str, str], engine: Engine) -> None:
    """§33, §35, §47, §49 — GENERATED ALWAYS ... STORED in model and database."""
    table_name, column_name = key
    column = METADATA.tables[table_name].c[column_name]
    assert isinstance(column.server_default, Computed), (
        f"{table_name}.{column_name} is not declared as Computed() in the model"
    )
    assert column.server_default.persisted is True, (
        f"{table_name}.{column_name} must be STORED, not VIRTUAL"
    )
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT is_generated, generation_expression
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table_name
                  AND column_name = :column_name
                """
            ),
            {"table_name": table_name, "column_name": column_name},
        ).one()
    assert row.is_generated == "ALWAYS", (
        f"{table_name}.{column_name} is not GENERATED in the database"
    )
    assert row.generation_expression, "generation expression is empty"


# ---------------------------------------------------------------------------
# 8. Primary keys
# ---------------------------------------------------------------------------
def test_primary_keys_match(db_tables: dict[str, dict]) -> None:
    problems: list[str] = []
    for table_name, table in sorted(METADATA.tables.items()):
        if table_name not in db_tables:
            continue
        model_pk = [c.name for c in table.primary_key.columns]
        db_pk = list(db_tables[table_name]["pk"]["constrained_columns"])
        if model_pk != db_pk:
            problems.append(f"{table_name}: database PK={db_pk} model PK={model_pk}")
    assert not problems, "\n".join(problems)


def test_user_roles_has_composite_pk() -> None:
    """§52 — user_roles is keyed by (user_id, role_id)."""
    table = METADATA.tables["user_roles"]
    assert [c.name for c in table.primary_key.columns] == ["user_id", "role_id"]


# ---------------------------------------------------------------------------
# 9. Foreign keys
# ---------------------------------------------------------------------------
def test_foreign_keys_match(db_tables: dict[str, dict]) -> None:
    """FK target and ON DELETE action must be identical (§16 depends on it)."""
    problems: list[str] = []
    for table_name, table in sorted(METADATA.tables.items()):
        if table_name not in db_tables:
            continue

        db_fks = {
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                (fk.get("options") or {}).get("ondelete", "").upper() or "NO ACTION",
            )
            for fk in db_tables[table_name]["fks"]
        }
        model_fks = set()
        for constraint in table.foreign_key_constraints:
            local = tuple(element.parent.name for element in constraint.elements)
            target_table = constraint.elements[0].column.table.name
            target_columns = tuple(element.column.name for element in constraint.elements)
            ondelete = (constraint.ondelete or "NO ACTION").upper()
            model_fks.add((local, target_table, target_columns, ondelete))

        if db_fks != model_fks:
            problems.append(
                f"{table_name}: missing in model {sorted(db_fks - model_fks)}; "
                f"absent in database {sorted(model_fks - db_fks)}"
            )
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# 10. UNIQUE constraints
# ---------------------------------------------------------------------------
def test_unique_constraints_match(db_tables: dict[str, dict]) -> None:
    problems: list[str] = []
    for table_name, table in sorted(METADATA.tables.items()):
        if table_name not in db_tables:
            continue
        db_uniques = {tuple(uc["column_names"]) for uc in db_tables[table_name]["uniques"]}
        model_uniques = {
            tuple(c.name for c in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        # Column-level unique=True does not create a UniqueConstraint object.
        for column in table.c:
            if column.unique:
                model_uniques.add((column.name,))
        if db_uniques != model_uniques:
            problems.append(
                f"{table_name}: missing in model {sorted(db_uniques - model_uniques)}; "
                f"absent in database {sorted(model_uniques - db_uniques)}"
            )
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# 11. CHECK constraints
# ---------------------------------------------------------------------------
def test_check_constraints_declared(db_tables: dict[str, dict]) -> None:
    """
    Every CHECK constraint that exists in the database is declared in the model.

    Only the names are compared: the SQL text is normalised by PostgreSQL
    (casts, parentheses, ARRAY rewriting) and comparing it literally would
    produce false failures. The semantics of every constraint are already
    exercised behaviourally by scripts/audit_step1.py.
    """
    problems: list[str] = []
    for table_name, table in sorted(METADATA.tables.items()):
        if table_name not in db_tables:
            continue
        db_checks = {
            check["name"] for check in db_tables[table_name]["checks"] if check.get("name")
        }
        model_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
        }
        missing = db_checks - model_checks
        if missing:
            problems.append(f"{table_name}: CHECK not declared in model: {sorted(missing)}")
        extra = model_checks - db_checks
        if extra:
            problems.append(f"{table_name}: CHECK absent in database: {sorted(extra)}")
    assert not problems, "\n".join(problems)


def test_check_constraint_count(db_tables: dict[str, dict]) -> None:
    """The schema contains 134 named CHECK constraints, including Transfer guards,
    the 14 bank-statement guards (G-BANK-2..5, migration 0020), the 9
    stock-ledger guards (6 stock_movements + 3 material_reservations, 0021)
    and the production_output quantity guard (G-C2, 0023)."""
    total = sum(len([c for c in data["checks"] if c.get("name")]) for data in db_tables.values())
    assert total == 134, f"expected 134 CHECK constraints in the database, found {total}"


# ---------------------------------------------------------------------------
# 12. EXCLUDE constraints
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,table_name", sorted(EXCLUDE_CONSTRAINTS.items()))
def test_exclude_constraints(name: str, table_name: str, engine: Engine) -> None:
    """§31, §34, §75 — non-overlapping periods are enforced by the database."""
    table = METADATA.tables[table_name]
    declared = {constraint.name for constraint in table.constraints}
    assert name in declared, f"{name} is not declared on model table {table_name}"

    with engine.connect() as connection:
        found = connection.execute(
            text(
                """
                SELECT 1
                FROM pg_constraint con
                JOIN pg_class c ON c.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND con.contype = 'x'
                  AND con.conname = :name
                  AND c.relname = :table_name
                """
            ),
            {"name": name, "table_name": table_name},
        ).first()
    assert found is not None, f"{name} does not exist in the database"


# ---------------------------------------------------------------------------
# 13. Indexes
# ---------------------------------------------------------------------------
def _model_index_names(table: Table) -> set[str]:
    return {index.name for index in table.indexes if index.name}


def test_indexes_declared(db_tables: dict[str, dict], engine: Engine) -> None:
    """
    Every non-constraint index of the database is declared in the model.

    Indexes that PostgreSQL creates implicitly for PRIMARY KEY / UNIQUE /
    EXCLUDE constraints are excluded from the comparison: they belong to the
    constraint, not to a separate Index() object.
    """
    with engine.connect() as connection:
        constraint_index_rows = connection.execute(
            text(
                """
                SELECT c.relname AS table_name, i.relname AS index_name
                FROM pg_constraint con
                JOIN pg_class c ON c.oid = con.conrelid
                JOIN pg_class i ON i.oid = con.conindid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND con.contype IN ('p', 'u', 'x')
                """
            )
        ).all()
    constraint_indexes: dict[str, set[str]] = {}
    for row in constraint_index_rows:
        constraint_indexes.setdefault(row.table_name, set()).add(row.index_name)

    problems: list[str] = []
    for table_name, table in sorted(METADATA.tables.items()):
        if table_name not in db_tables:
            continue
        implicit = constraint_indexes.get(table_name, set())
        db_indexes = {
            index["name"]
            for index in db_tables[table_name]["indexes"]
            if index.get("name") and index["name"] not in implicit
        }
        model_indexes = _model_index_names(table) - implicit
        missing = db_indexes - model_indexes
        if missing:
            problems.append(f"{table_name}: index not declared in model: {sorted(missing)}")
        extra = model_indexes - db_indexes
        if extra:
            problems.append(f"{table_name}: index absent in database: {sorted(extra)}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("index_name,table_name", sorted(MANDATORY_INDEXES.items()))
def test_mandatory_indexes(index_name: str, table_name: str, engine: Engine) -> None:
    """§64 — the four mandatory indexes exist with exactly these names."""
    table = METADATA.tables[table_name]
    assert index_name in _model_index_names(table), (
        f"{index_name} is not declared on model table {table_name}"
    )
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = :table_name "
                "AND indexname = :index_name"
            ),
            {"table_name": table_name, "index_name": index_name},
        ).first()
    assert found is not None, f"{index_name} does not exist in the database"


@pytest.mark.parametrize("index_name,table_name", sorted(IDEMPOTENCY_INDEXES.items()))
def test_idempotency_indexes_are_partial_unique(
    index_name: str,
    table_name: str,
    engine: Engine,
) -> None:
    """§41, §57, ADR-010 — idempotency relies on partial UNIQUE indexes."""
    table = METADATA.tables[table_name]
    declared = {index.name: index for index in table.indexes if index.name}
    index: Index | None = declared.get(index_name)
    assert index is not None, f"{index_name} is not declared on model table {table_name}"
    assert index.unique is True, f"{index_name} must be UNIQUE in the model"

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = :index_name"
            ),
            {"index_name": index_name},
        ).first()
    assert row is not None, f"{index_name} does not exist in the database"
    assert row.indexdef.startswith("CREATE UNIQUE INDEX"), (
        f"{index_name} is not UNIQUE in the database: {row.indexdef}"
    )
    assert " WHERE " in row.indexdef, f"{index_name} must be a PARTIAL index: {row.indexdef}"


# ---------------------------------------------------------------------------
# 14. Views
# ---------------------------------------------------------------------------
def test_views_exist_and_are_not_mapped(engine: Engine) -> None:
    """§63 — the five views exist in the database and are not mapped as tables."""
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT table_name FROM information_schema.views WHERE table_schema='public'")
        ).all()
    database_views = {row.table_name for row in rows}
    missing = set(VIEW_NAMES) - database_views
    assert not missing, f"views missing from the database: {sorted(missing)}"

    wrongly_mapped = set(VIEW_NAMES) & set(METADATA.tables)
    assert not wrongly_mapped, f"views must not be mapped as tables: {sorted(wrongly_mapped)}"
