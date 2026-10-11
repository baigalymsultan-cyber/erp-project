#!/usr/bin/env python3
"""
DAY 2 INDEPENDENT VERIFICATION PASS.

MASTER PROMPT §4, §90, §91: a step may not be declared complete while any
missing requirement exists, and every guarantee must be verified by an
automated check rather than by inspection.

WHY THIS SCRIPT EXISTS
----------------------
tests/test_models_match_schema.py compares SQLAlchemy metadata against a live
database by reading *Python attributes* of the reflected columns. That catches
a lot, but it shares a blind spot with the code under test: both sides are
interpreted by the same SQLAlchemy reflection layer, and neither side is ever
compared with what `database/schema.sql` actually *says*.

This script closes that gap by comparing THREE independent sources:

  A. database/schema.sql, parsed with pglast — the real PostgreSQL grammar
     (libpg_query), not a regex and not SQLAlchemy;
  B. a PostgreSQL catalog produced by applying schema.sql with psql;
  C. a PostgreSQL catalog produced by SQLAlchemy's own DDL compiler via
     Base.metadata.create_all() on an empty database.

B vs C is the decisive comparison: both sides are dumped from pg_catalog by
identical queries, so a difference is a real physical difference between the
schema and the models — it cannot be an artefact of how SQLAlchemy renders a
Python type. A is used to prove that B is genuinely derived from the reviewed
file and that the file itself declares what we claim it declares.

Usage
-----
    scripts/verify_day2.py                 # uses the default verify_* databases
    VERIFY_SQL_DB=... VERIFY_MODELS_DB=... VERIFY_ALEMBIC_DB=... scripts/verify_day2.py

Exit code 0 means every category matched.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg
from pglast import enums, parse_sql

PROJECT_ROOT = Path(__file__).resolve().parents[0]
SCHEMA_FILE = PROJECT_ROOT / "database" / "schema.sql"

HOST = os.environ.get("VERIFY_DB_HOST", "127.0.0.1")
PORT = os.environ.get("VERIFY_DB_PORT", "5432")
USER = os.environ.get("VERIFY_DB_USER", "erp_app")
PASSWORD = os.environ.get("VERIFY_DB_PASSWORD", "devpass_local_only")

DB_SQL = os.environ.get("VERIFY_SQL_DB", "verify_sql")
DB_MODELS = os.environ.get("VERIFY_MODELS_DB", "verify_models")
DB_ALEMBIC = os.environ.get("VERIFY_ALEMBIC_DB", "verify_alembic")

#: Tables owned by Alembic, deliberately not mapped as ORM models.
INFRASTRUCTURE_TABLES = frozenset({"alembic_version"})

#: Expected inventory, taken from the STEP 1 report. Restating it here means a
#: silent change to the schema is reported instead of being absorbed.
EXPECTED = {
    "tables": 31,
    "original_tables": 17,
    "extension_tables": 14,
    "views": 5,
    "functions": 11,
    "triggers": 59,
    "indexes": 116,
    "checks": 105,
    "foreign_keys": 60,
    "excludes": 3,
    "generated_columns": 4,
    "extensions": ["btree_gist"],
}

#: §80 — the 17 tables of the original specification, in specification order.
ORIGINAL_TABLES = (
    "ip_entities",
    "operation_types",
    "categories",
    "counterparties",
    "accounts",
    "operations",
    "transfers",
    "employees",
    "work_operation_types",
    "rates_history",
    "work_entries",
    "payroll_periods",
    "payroll_summary",
    "orders",
    "order_items",
    "products",
    "materials",
)


def dsn(database: str) -> str:
    return f"postgresql://{USER}:{PASSWORD}@{HOST}:{PORT}/{database}"


# =====================================================================
# Reporting
# =====================================================================
class Report:
    """Collects check results; prints a category-grouped summary."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.category = "(none)"
        self.failures: list[tuple[str, str]] = []
        self.evidence: list[str] = []

    def section(self, name: str) -> None:
        self.category = name
        print(f"\n--- {name} " + "-" * max(0, 66 - len(name)))

    def ok(self, message: str) -> None:
        self.passed += 1
        print(f"  PASS  {message}")

    def fail(self, message: str) -> None:
        self.failed += 1
        self.failures.append((self.category, message))
        print(f"  FAIL  {message}")

    def check(self, condition: bool, message: str) -> bool:
        if condition:
            self.ok(message)
        else:
            self.fail(message)
        return condition

    def note(self, message: str) -> None:
        self.evidence.append(message)
        print(f"  ....  {message}")

    def compare_sets(
        self, label: str, left: set[str], right: set[str], left_name: str, right_name: str
    ) -> bool:
        """Compare two sets and report the actual difference, not just a verdict."""
        only_left = sorted(left - right)
        only_right = sorted(right - left)
        if not only_left and not only_right:
            return self.check(True, f"{label}: identical ({len(left)} items)")
        detail = []
        if only_left:
            detail.append(f"only in {left_name}: {only_left}")
        if only_right:
            detail.append(f"only in {right_name}: {only_right}")
        return self.check(False, f"{label}: DIFFERS — " + "; ".join(detail))


# =====================================================================
# Catalog dump — identical queries against every database
# =====================================================================
CATALOG_QUERIES: dict[str, str] = {
    "columns": """
        SELECT c.relname AS table_name,
               a.attname AS column_name,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               a.attnotnull AS not_null,
               COALESCE(pg_get_expr(d.adbin, d.adrelid), '') AS default_expr,
               a.attgenerated AS generated,
               a.attidentity AS identity
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'
           AND a.attnum > 0
           AND NOT a.attisdropped
         ORDER BY c.relname, a.attnum
    """,
    "constraints": """
        SELECT c.relname AS table_name,
               con.conname AS constraint_name,
               con.contype AS constraint_type,
               pg_get_constraintdef(con.oid) AS definition
          FROM pg_constraint con
          JOIN pg_class c ON c.oid = con.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
         ORDER BY c.relname, con.conname
    """,
    "indexes": """
        SELECT tablename AS table_name,
               indexname AS index_name,
               indexdef AS definition
          FROM pg_indexes
         WHERE schemaname = 'public'
         ORDER BY tablename, indexname
    """,
    "triggers": """
        SELECT c.relname AS table_name,
               t.tgname AS trigger_name,
               pg_get_triggerdef(t.oid) AS definition
          FROM pg_trigger t
          JOIN pg_class c ON c.oid = t.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND NOT t.tgisinternal
         ORDER BY c.relname, t.tgname
    """,
    # Functions owned by an installed EXTENSION are excluded. btree_gist alone
    # installs 188 support functions into the public schema; counting them as
    # project objects would both inflate the inventory and mask a missing
    # fn_* function behind a large number.
    "functions": """
        SELECT p.proname AS function_name,
               pg_get_functiondef(p.oid) AS definition
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND p.prokind = 'f'
           AND NOT EXISTS (
               SELECT 1
                 FROM pg_depend dep
                WHERE dep.objid = p.oid
                  AND dep.deptype = 'e'
           )
         ORDER BY p.proname
    """,
    "views": """
        SELECT c.relname AS view_name,
               pg_get_viewdef(c.oid, TRUE) AS definition
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'v'
         ORDER BY c.relname
    """,
    "extensions": """
        SELECT extname AS extension_name FROM pg_extension ORDER BY extname
    """,
}


def dump_catalog(database: str) -> dict[str, list[dict[str, Any]]]:
    """Dump the physical catalog of *database* using the shared queries."""
    result: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(dsn(database)) as conn:
        for name, query in CATALOG_QUERIES.items():
            with conn.cursor() as cur:
                cur.execute(query)
                columns = [d.name for d in cur.description or []]
                rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
            result[name] = rows
    return result


def user_tables(catalog: dict[str, list[dict[str, Any]]]) -> set[str]:
    return {
        row["table_name"]
        for row in catalog["columns"]
        if row["table_name"] not in INFRASTRUCTURE_TABLES
    }


def column_map(catalog: dict[str, list[dict[str, Any]]]) -> dict[tuple[str, str], dict]:
    return {
        (row["table_name"], row["column_name"]): row
        for row in catalog["columns"]
        if row["table_name"] not in INFRASTRUCTURE_TABLES
    }


def constraint_map(
    catalog: dict[str, list[dict[str, Any]]], types: str
) -> dict[tuple[str, str], str]:
    return {
        (row["table_name"], row["constraint_name"]): row["definition"]
        for row in catalog["constraints"]
        if row["constraint_type"] in types
        and row["table_name"] not in INFRASTRUCTURE_TABLES
    }


def index_map(catalog: dict[str, list[dict[str, Any]]]) -> dict[tuple[str, str], str]:
    return {
        (row["table_name"], row["index_name"]): row["definition"]
        for row in catalog["indexes"]
        if row["table_name"] not in INFRASTRUCTURE_TABLES
    }


def normalise_default(expression: str) -> str:
    """
    Reduce a default expression to a comparable form.

    A BIGSERIAL column's default names its own sequence
    (nextval('orders_id_seq'::regclass)); the sequence name embeds the table
    name, so the raw strings of two different databases are equal anyway. The
    only normalisation needed is whitespace and the now/CURRENT_TIMESTAMP pair,
    which PostgreSQL itself stores canonically.
    """
    return " ".join(expression.split())


# =====================================================================
# A. schema.sql parsed with pglast (real PostgreSQL grammar)
# =====================================================================
class ParsedSchema:
    """Facts extracted from database/schema.sql by the PostgreSQL parser."""

    def __init__(self, sql: str) -> None:
        self.tables: set[str] = set()
        self.table_columns: dict[str, list[str]] = defaultdict(list)
        self.views: set[str] = set()
        self.functions: set[str] = set()
        self.triggers: dict[str, str] = {}
        self.extensions: set[str] = set()
        self.named_constraints: set[tuple[str, str]] = set()
        self.indexes: set[str] = set()
        self.generated_columns: set[tuple[str, str]] = set()
        self.statement_count = 0
        #: Triggers created inside DO $$ ... $$ blocks by FOREACH + format().
        #: pglast sees a DoStmt and cannot look inside the PL/pgSQL body, so
        #: those trigger names are recovered from the block separately (see
        #: _do_block). 52 of the 59 triggers are created this way.
        self.dynamic_triggers: dict[str, str] = {}
        self.do_blocks = 0

        # pglast parses SQL, not psql meta-commands; drop the client directive
        # exactly as the Alembic revision does (BUG-002).
        cleaned = "\n".join(
            "" if line.lstrip().startswith("\\") else line
            for line in sql.splitlines()
        )

        for raw in parse_sql(cleaned):
            statement = raw.stmt
            self.statement_count += 1
            self._visit(statement)

    def _visit(self, statement: Any) -> None:
        kind = type(statement).__name__
        if kind == "CreateStmt":
            self._create_table(statement)
        elif kind == "ViewStmt":
            self.views.add(statement.view.relname)
        elif kind == "CreateFunctionStmt":
            name = statement.funcname[-1].sval
            self.functions.add(name)
        elif kind == "CreateTrigStmt":
            self.triggers[statement.trigname] = statement.relation.relname
        elif kind == "CreateExtensionStmt":
            self.extensions.add(statement.extname)
        elif kind == "IndexStmt":
            if statement.idxname:
                self.indexes.add(statement.idxname)
        elif kind == "AlterTableStmt":
            self._alter_table(statement)
        elif kind == "DoStmt":
            self._do_block(statement)

    def _do_block(self, statement: Any) -> None:
        """
        Recover trigger names created dynamically inside a DO $$ ... $$ block.

        The block declares a table array and executes
        `format('CREATE TRIGGER trg_%s_<suffix> ... ON %I', v_table, v_table)`
        for each element, so the resulting trigger names are the cartesian
        product of the array and the template. The array literal and the
        template are extracted from the block's source text, which is what
        pglast exposes for a DoStmt.

        This is parsing of a PL/pgSQL body and is therefore the one place in
        this script that is not backed by the SQL grammar. It is still
        independent of the models: the recovered names are compared against
        pg_trigger, so a wrong recovery shows up as a mismatch rather than as
        a silent pass.
        """
        self.do_blocks += 1
        body = ""
        for argument in statement.args or ():
            if getattr(argument, "defname", None) == "as":
                body = argument.arg.sval
                break
        if not body:
            return

        tables: list[str] = []
        array_start = body.find("ARRAY[")
        if array_start != -1:
            array_end = body.find("]", array_start)
            literal = body[array_start + len("ARRAY[") : array_end]
            tables = [
                part.strip().strip("'")
                for part in literal.split(",")
                if part.strip().strip("'")
            ]

        for line in body.splitlines():
            marker = "'CREATE TRIGGER trg_%s_"
            position = line.find(marker)
            if position == -1:
                continue
            remainder = line[position + len(marker) :]
            suffix = ""
            for character in remainder:
                if character.isalnum() or character == "_":
                    suffix += character
                else:
                    break
            if not suffix:
                continue
            for table in tables:
                self.dynamic_triggers[f"trg_{table}_{suffix}"] = table

    @property
    def all_triggers(self) -> dict[str, str]:
        """Static CREATE TRIGGER statements plus the ones built by DO blocks."""
        return {**self.triggers, **self.dynamic_triggers}

    def _create_table(self, statement: Any) -> None:
        table = statement.relation.relname
        self.tables.add(table)
        for element in statement.tableElts or ():
            element_kind = type(element).__name__
            if element_kind == "ColumnDef":
                self.table_columns[table].append(element.colname)
                for constraint in element.constraints or ():
                    # The enum member must be compared directly: str() of a
                    # pglast ConstrType yields its numeric value ("'4'"), so a
                    # substring test for "GENERATED" silently never matches.
                    if constraint.contype == enums.ConstrType.CONSTR_GENERATED:
                        self.generated_columns.add((table, element.colname))
                    if constraint.conname:
                        self.named_constraints.add((table, constraint.conname))
            elif element_kind == "Constraint" and element.conname:
                self.named_constraints.add((table, element.conname))

    def _alter_table(self, statement: Any) -> None:
        table = statement.relation.relname
        for command in statement.cmds or ():
            definition = getattr(command, "def_", None)
            if definition is None:
                continue
            if type(definition).__name__ == "Constraint" and definition.conname:
                self.named_constraints.add((table, definition.conname))


# =====================================================================
# Verification categories
# =====================================================================
def verify_parse(report: Report, parsed: ParsedSchema) -> None:
    report.section("1. schema.sql parsed by the PostgreSQL grammar (pglast)")
    report.note(
        f"parsed {parsed.statement_count} top-level statements from {SCHEMA_FILE.name}"
    )
    report.check(
        len(parsed.tables) == EXPECTED["tables"],
        f"CREATE TABLE statements: {len(parsed.tables)} (expected {EXPECTED['tables']})",
    )
    report.check(
        len(parsed.views) == EXPECTED["views"],
        f"CREATE VIEW statements: {len(parsed.views)} (expected {EXPECTED['views']})",
    )
    report.check(
        len(parsed.functions) == EXPECTED["functions"],
        f"CREATE FUNCTION statements: {len(parsed.functions)} "
        f"(expected {EXPECTED['functions']})",
    )
    report.note(
        f"{len(parsed.triggers)} triggers are created by static CREATE TRIGGER "
        f"statements; {len(parsed.dynamic_triggers)} more are created inside "
        f"{parsed.do_blocks} DO $$ blocks (§56/§16/§54 apply the same trigger "
        "to a list of tables)"
    )
    report.check(
        len(parsed.all_triggers) == EXPECTED["triggers"],
        f"triggers declared in the file: {len(parsed.all_triggers)} "
        f"(expected {EXPECTED['triggers']})",
    )
    report.check(
        sorted(parsed.extensions) == EXPECTED["extensions"],
        f"CREATE EXTENSION statements: {sorted(parsed.extensions)}",
    )
    report.check(
        len(parsed.generated_columns) == EXPECTED["generated_columns"],
        f"GENERATED columns declared in the file: "
        f"{sorted(parsed.generated_columns)}",
    )
    missing_original = [t for t in ORIGINAL_TABLES if t not in parsed.tables]
    report.check(
        not missing_original,
        "§80/§81 — all 17 original tables present in the file"
        + (f"; MISSING: {missing_original}" if missing_original else ""),
    )


def verify_file_vs_catalog(
    report: Report, parsed: ParsedSchema, catalog_sql: dict
) -> None:
    report.section("2. schema.sql (parsed) vs PostgreSQL catalog after psql")
    report.compare_sets(
        "table set", parsed.tables, user_tables(catalog_sql), "file", "catalog"
    )

    catalog_views = {row["view_name"] for row in catalog_sql["views"]}
    report.compare_sets("view set", parsed.views, catalog_views, "file", "catalog")

    catalog_functions = {row["function_name"] for row in catalog_sql["functions"]}
    report.compare_sets(
        "function set", parsed.functions, catalog_functions, "file", "catalog"
    )

    catalog_triggers = {row["trigger_name"] for row in catalog_sql["triggers"]}
    report.compare_sets(
        "trigger set", set(parsed.all_triggers), catalog_triggers, "file", "catalog"
    )
    # The table each trigger sits on must match too, otherwise a trigger could
    # be attached to the wrong table and the name comparison would still pass.
    catalog_trigger_tables = {
        row["trigger_name"]: row["table_name"] for row in catalog_sql["triggers"]
    }
    wrong_table = [
        f"{name}: file says {table}, catalog says {catalog_trigger_tables.get(name)}"
        for name, table in sorted(parsed.all_triggers.items())
        if catalog_trigger_tables.get(name) != table
    ]
    report.check(
        not wrong_table,
        f"every trigger is attached to the table the file names "
        f"({len(parsed.all_triggers)} checked)"
        + (f"; MISMATCHED: {wrong_table}" if wrong_table else ""),
    )

    catalog_extensions = {row["extension_name"] for row in catalog_sql["extensions"]}
    report.check(
        parsed.extensions <= catalog_extensions,
        f"extensions from the file are installed: {sorted(parsed.extensions)}",
    )

    # Column-by-column, per table: the file's declaration order must match the
    # catalog's attribute order, otherwise a column was added or reordered.
    catalog_columns: dict[str, list[str]] = defaultdict(list)
    for row in catalog_sql["columns"]:
        if row["table_name"] not in INFRASTRUCTURE_TABLES:
            catalog_columns[row["table_name"]].append(row["column_name"])

    mismatched = []
    for table in sorted(parsed.tables):
        if parsed.table_columns[table] != catalog_columns.get(table, []):
            mismatched.append(table)
    report.check(
        not mismatched,
        "column names and order match for all 31 tables"
        + (f"; MISMATCHED: {mismatched}" if mismatched else ""),
    )

    catalog_generated = {
        (row["table_name"], row["column_name"])
        for row in catalog_sql["columns"]
        if row["generated"] == "s"
    }
    report.compare_sets(
        "GENERATED columns",
        {f"{t}.{c}" for t, c in parsed.generated_columns},
        {f"{t}.{c}" for t, c in catalog_generated},
        "file",
        "catalog",
    )

    catalog_named = {
        (row["table_name"], row["constraint_name"])
        for row in catalog_sql["constraints"]
        if row["table_name"] not in INFRASTRUCTURE_TABLES
    }
    missing_in_catalog = sorted(parsed.named_constraints - catalog_named)
    report.check(
        not missing_in_catalog,
        f"every explicitly named constraint in the file exists in the catalog "
        f"({len(parsed.named_constraints)} checked)"
        + (f"; MISSING: {missing_in_catalog}" if missing_in_catalog else ""),
    )

    catalog_index_names = {row["index_name"] for row in catalog_sql["indexes"]}
    missing_indexes = sorted(parsed.indexes - catalog_index_names)
    report.check(
        not missing_indexes,
        f"every CREATE INDEX from the file exists in the catalog "
        f"({len(parsed.indexes)} checked)"
        + (f"; MISSING: {missing_indexes}" if missing_indexes else ""),
    )


def verify_inventory(report: Report, catalog_sql: dict) -> None:
    report.section("3. Catalog inventory against the declared STEP 1 numbers")
    tables = user_tables(catalog_sql)
    report.check(
        len(tables) == EXPECTED["tables"], f"tables: {len(tables)}"
    )

    original_present = [t for t in ORIGINAL_TABLES if t in tables]
    report.check(
        len(original_present) == EXPECTED["original_tables"],
        f"§80/§81 original tables: {len(original_present)}/17",
    )
    report.check(
        len(tables) - len(original_present) == EXPECTED["extension_tables"],
        f"§82 extension tables: {len(tables) - len(original_present)}",
    )

    for key, contype in (("checks", "c"), ("foreign_keys", "f"), ("excludes", "x")):
        found = len(constraint_map(catalog_sql, contype))
        report.check(found == EXPECTED[key], f"{key}: {found} (expected {EXPECTED[key]})")

    for key, source, _field in (
        ("views", "views", "view_name"),
        ("functions", "functions", "function_name"),
        ("triggers", "triggers", "trigger_name"),
    ):
        found = len(catalog_sql[source])
        report.check(found == EXPECTED[key], f"{key}: {found} (expected {EXPECTED[key]})")

    indexes = len(index_map(catalog_sql))
    report.check(
        indexes == EXPECTED["indexes"], f"indexes: {indexes} (expected {EXPECTED['indexes']})"
    )

    generated = len([r for r in catalog_sql["columns"] if r["generated"] == "s"])
    report.check(
        generated == EXPECTED["generated_columns"],
        f"generated columns: {generated} (expected {EXPECTED['generated_columns']})",
    )


def verify_models_vs_schema(
    report: Report, catalog_sql: dict, catalog_models: dict
) -> None:
    """
    THE decisive comparison: SQLAlchemy's own DDL output vs schema.sql output.

    Both catalogs come from the same queries against real PostgreSQL, so any
    difference here is a physical difference between the models and the schema.
    """
    report.section("4. SQLAlchemy metadata vs schema.sql — physical catalogs")

    report.compare_sets(
        "table set",
        user_tables(catalog_sql),
        user_tables(catalog_models),
        "schema.sql",
        "models",
    )

    sql_columns = column_map(catalog_sql)
    model_columns = column_map(catalog_models)
    report.compare_sets(
        "column set",
        {f"{t}.{c}" for t, c in sql_columns},
        {f"{t}.{c}" for t, c in model_columns},
        "schema.sql",
        "models",
    )

    shared = sorted(set(sql_columns) & set(model_columns))

    type_diffs = [
        f"{t}.{c}: schema={sql_columns[(t, c)]['data_type']} "
        f"models={model_columns[(t, c)]['data_type']}"
        for t, c in shared
        if sql_columns[(t, c)]["data_type"] != model_columns[(t, c)]["data_type"]
    ]
    report.check(
        not type_diffs,
        f"physical column types identical ({len(shared)} columns compared)"
        + (f"; DIFFERENCES: {type_diffs}" if type_diffs else ""),
    )

    null_diffs = [
        f"{t}.{c}: schema not_null={sql_columns[(t, c)]['not_null']} "
        f"models not_null={model_columns[(t, c)]['not_null']}"
        for t, c in shared
        if sql_columns[(t, c)]["not_null"] != model_columns[(t, c)]["not_null"]
    ]
    report.check(
        not null_diffs,
        f"NOT NULL identical ({len(shared)} columns compared)"
        + (f"; DIFFERENCES: {null_diffs}" if null_diffs else ""),
    )

    default_diffs = [
        f"{t}.{c}: schema={sql_columns[(t, c)]['default_expr']!r} "
        f"models={model_columns[(t, c)]['default_expr']!r}"
        for t, c in shared
        if normalise_default(sql_columns[(t, c)]["default_expr"])
        != normalise_default(model_columns[(t, c)]["default_expr"])
    ]
    report.check(
        not default_diffs,
        f"server defaults identical ({len(shared)} columns compared)"
        + (f"; DIFFERENCES: {default_diffs}" if default_diffs else ""),
    )

    generated_diffs = [
        f"{t}.{c}: schema={sql_columns[(t, c)]['generated']!r} "
        f"models={model_columns[(t, c)]['generated']!r}"
        for t, c in shared
        if sql_columns[(t, c)]["generated"] != model_columns[(t, c)]["generated"]
    ]
    report.check(
        not generated_diffs,
        "GENERATED ... STORED status identical"
        + (f"; DIFFERENCES: {generated_diffs}" if generated_diffs else ""),
    )

    identity_diffs = [
        f"{t}.{c}: schema={sql_columns[(t, c)]['identity']!r} "
        f"models={model_columns[(t, c)]['identity']!r}"
        for t, c in shared
        if sql_columns[(t, c)]["identity"] != model_columns[(t, c)]["identity"]
    ]
    report.check(
        not identity_diffs,
        "IDENTITY/SERIAL strategy identical"
        + (f"; DIFFERENCES: {identity_diffs}" if identity_diffs else ""),
    )


def verify_constraints_models_vs_schema(
    report: Report, catalog_sql: dict, catalog_models: dict
) -> None:
    report.section("5. Constraints — definition text, per type")

    labels = {
        "p": "PRIMARY KEY",
        "u": "UNIQUE",
        "c": "CHECK",
        "f": "FOREIGN KEY (incl. ON DELETE / ON UPDATE actions)",
        "x": "EXCLUDE",
    }
    for contype, label in labels.items():
        left = constraint_map(catalog_sql, contype)
        right = constraint_map(catalog_models, contype)
        report.compare_sets(
            f"{label} — names",
            {f"{t}.{n}" for t, n in left},
            {f"{t}.{n}" for t, n in right},
            "schema.sql",
            "models",
        )
        shared = sorted(set(left) & set(right))
        diffs = [
            f"{t}.{n}:\n        schema.sql: {left[(t, n)]}\n        models    : {right[(t, n)]}"
            for t, n in shared
            if " ".join(left[(t, n)].split()) != " ".join(right[(t, n)].split())
        ]
        report.check(
            not diffs,
            f"{label} — definitions identical ({len(shared)} compared)"
            + ("\n      " + "\n      ".join(diffs) if diffs else ""),
        )


def verify_indexes_models_vs_schema(
    report: Report, catalog_sql: dict, catalog_models: dict
) -> None:
    report.section("6. Indexes — definition text (incl. partial WHERE, USING, order)")
    left = index_map(catalog_sql)
    right = index_map(catalog_models)
    report.compare_sets(
        "index names",
        {f"{t}.{n}" for t, n in left},
        {f"{t}.{n}" for t, n in right},
        "schema.sql",
        "models",
    )
    shared = sorted(set(left) & set(right))
    diffs = [
        f"{t}.{n}:\n        schema.sql: {left[(t, n)]}\n        models    : {right[(t, n)]}"
        for t, n in shared
        if " ".join(left[(t, n)].split()) != " ".join(right[(t, n)].split())
    ]
    report.check(
        not diffs,
        f"index definitions identical ({len(shared)} compared)"
        + ("\n      " + "\n      ".join(diffs) if diffs else ""),
    )


def verify_objects_not_in_models(
    report: Report, catalog_sql: dict, catalog_models: dict
) -> None:
    """
    Triggers, functions and views exist only in schema.sql — by design.

    ADR-011 accepts this asymmetry, but it must be *stated and measured*: if
    someone ever runs create_all() against a production database believing the
    models are complete, the result is a schema without a single business rule.
    """
    report.section("7. Trigger / function / view coverage (ADR-011 asymmetry)")

    for source, field, label in (
        ("triggers", "trigger_name", "triggers"),
        ("functions", "function_name", "functions"),
        ("views", "view_name", "views"),
    ):
        in_schema = {row[field] for row in catalog_sql[source]}
        in_models = {row[field] for row in catalog_models[source]}
        report.check(
            len(in_schema) == EXPECTED[label],
            f"{label} present in the schema.sql database: {len(in_schema)}",
        )
        report.check(
            not in_models,
            f"{label} absent from the create_all() database: {len(in_models)} found "
            "— expected 0, because SQLAlchemy metadata cannot express them "
            "(ADR-011: create_all() must never be used to provision this system)",
        )


def verify_alembic_equals_psql(
    report: Report, catalog_sql: dict, catalog_alembic: dict
) -> None:
    report.section("8. Alembic-created database vs psql-created database")

    for source, field in (
        ("views", "view_name"),
        ("functions", "function_name"),
        ("triggers", "trigger_name"),
    ):
        left = {row[field] for row in catalog_sql[source]}
        right = {row[field] for row in catalog_alembic[source]}
        report.compare_sets(f"{source}", left, right, "psql", "alembic")

    report.compare_sets(
        "tables",
        user_tables(catalog_sql),
        user_tables(catalog_alembic),
        "psql",
        "alembic",
    )

    left_columns = column_map(catalog_sql)
    right_columns = column_map(catalog_alembic)
    report.compare_sets(
        "columns",
        {f"{t}.{c}" for t, c in left_columns},
        {f"{t}.{c}" for t, c in right_columns},
        "psql",
        "alembic",
    )

    column_diffs = [
        f"{t}.{c}"
        for t, c in sorted(set(left_columns) & set(right_columns))
        if (
            left_columns[(t, c)]["data_type"],
            left_columns[(t, c)]["not_null"],
            normalise_default(left_columns[(t, c)]["default_expr"]),
            left_columns[(t, c)]["generated"],
        )
        != (
            right_columns[(t, c)]["data_type"],
            right_columns[(t, c)]["not_null"],
            normalise_default(right_columns[(t, c)]["default_expr"]),
            right_columns[(t, c)]["generated"],
        )
    ]
    report.check(
        not column_diffs,
        f"column type/nullability/default/generated identical "
        f"({len(set(left_columns) & set(right_columns))} compared)"
        + (f"; DIFFERENCES: {column_diffs}" if column_diffs else ""),
    )

    for contype, label in (
        ("p", "PRIMARY KEY"),
        ("u", "UNIQUE"),
        ("c", "CHECK"),
        ("f", "FOREIGN KEY"),
        ("x", "EXCLUDE"),
    ):
        left = constraint_map(catalog_sql, contype)
        right = constraint_map(catalog_alembic, contype)
        differing = [
            f"{t}.{n}"
            for t, n in sorted(set(left) & set(right))
            if " ".join(left[(t, n)].split()) != " ".join(right[(t, n)].split())
        ]
        report.check(
            set(left) == set(right) and not differing,
            f"{label}: {len(left)} constraints identical"
            + (f"; DIFFERENCES: {differing}" if differing else ""),
        )

    left_indexes = index_map(catalog_sql)
    right_indexes = index_map(catalog_alembic)
    differing = [
        f"{t}.{n}"
        for t, n in sorted(set(left_indexes) & set(right_indexes))
        if " ".join(left_indexes[(t, n)].split())
        != " ".join(right_indexes[(t, n)].split())
    ]
    report.check(
        set(left_indexes) == set(right_indexes) and not differing,
        f"indexes: {len(left_indexes)} identical"
        + (f"; DIFFERENCES: {differing}" if differing else ""),
    )

    trigger_defs_left = {
        (row["table_name"], row["trigger_name"]): row["definition"]
        for row in catalog_sql["triggers"]
    }
    trigger_defs_right = {
        (row["table_name"], row["trigger_name"]): row["definition"]
        for row in catalog_alembic["triggers"]
    }
    differing = [
        f"{t}.{n}"
        for t, n in sorted(set(trigger_defs_left) & set(trigger_defs_right))
        if trigger_defs_left[(t, n)] != trigger_defs_right[(t, n)]
    ]
    report.check(
        not differing,
        f"trigger definitions identical ({len(trigger_defs_left)} compared)"
        + (f"; DIFFERENCES: {differing}" if differing else ""),
    )

    function_defs_left = {
        row["function_name"]: row["definition"] for row in catalog_sql["functions"]
    }
    function_defs_right = {
        row["function_name"]: row["definition"] for row in catalog_alembic["functions"]
    }
    differing = [
        name
        for name in sorted(set(function_defs_left) & set(function_defs_right))
        if function_defs_left[name] != function_defs_right[name]
    ]
    report.check(
        not differing,
        f"function bodies identical ({len(function_defs_left)} compared)"
        + (f"; DIFFERENCES: {differing}" if differing else ""),
    )

    view_defs_left = {row["view_name"]: row["definition"] for row in catalog_sql["views"]}
    view_defs_right = {
        row["view_name"]: row["definition"] for row in catalog_alembic["views"]
    }
    differing = [
        name
        for name in sorted(set(view_defs_left) & set(view_defs_right))
        if view_defs_left[name] != view_defs_right[name]
    ]
    report.check(
        not differing,
        f"view definitions identical ({len(view_defs_left)} compared)"
        + (f"; DIFFERENCES: {differing}" if differing else ""),
    )


def verify_fk_actions(report: Report, catalog_sql: dict) -> None:
    """
    §16/§18 — referential actions are a data-safety requirement, not a detail.

    A financial ledger must never lose its parent silently, so the distribution
    of ON DELETE actions is reported explicitly rather than assumed.
    """
    report.section("9. Foreign key referential actions")
    foreign_keys = constraint_map(catalog_sql, "f")
    buckets: dict[str, list[str]] = defaultdict(list)
    for (table, name), definition in sorted(foreign_keys.items()):
        upper = definition.upper()
        if "ON DELETE CASCADE" in upper:
            action = "CASCADE"
        elif "ON DELETE SET NULL" in upper:
            action = "SET NULL"
        elif "ON DELETE RESTRICT" in upper:
            action = "RESTRICT"
        elif "ON DELETE SET DEFAULT" in upper:
            action = "SET DEFAULT"
        else:
            action = "NO ACTION (default)"
        buckets[action].append(f"{table}.{name}")

    total = sum(len(v) for v in buckets.values())
    report.check(
        total == EXPECTED["foreign_keys"],
        f"foreign keys classified: {total} (expected {EXPECTED['foreign_keys']})",
    )
    for action in sorted(buckets):
        report.note(f"ON DELETE {action}: {len(buckets[action])}")

    deferrable = [
        f"{t}.{n}" for (t, n), d in foreign_keys.items() if "DEFERRABLE" in d.upper()
    ]
    report.note(f"DEFERRABLE foreign keys: {len(deferrable)}")


def main() -> int:
    if not SCHEMA_FILE.is_file():
        print(f"ERROR: {SCHEMA_FILE} not found", file=sys.stderr)
        return 2

    print("=" * 72)
    print("DAY 2 INDEPENDENT VERIFICATION PASS (§4, §90, §91)")
    print("=" * 72)
    print(f"  A. file    : {SCHEMA_FILE} (parsed with pglast / libpg_query)")
    print(f"  B. catalog : {DB_SQL}      (psql -f schema.sql)")
    print(f"  C. catalog : {DB_MODELS}   (Base.metadata.create_all)")
    print(f"  D. catalog : {DB_ALEMBIC}  (alembic upgrade head)")

    report = Report()

    parsed = ParsedSchema(SCHEMA_FILE.read_text(encoding="utf-8"))
    catalog_sql = dump_catalog(DB_SQL)
    catalog_models = dump_catalog(DB_MODELS)
    catalog_alembic = dump_catalog(DB_ALEMBIC)

    verify_parse(report, parsed)
    verify_file_vs_catalog(report, parsed, catalog_sql)
    verify_inventory(report, catalog_sql)
    verify_models_vs_schema(report, catalog_sql, catalog_models)
    verify_constraints_models_vs_schema(report, catalog_sql, catalog_models)
    verify_indexes_models_vs_schema(report, catalog_sql, catalog_models)
    verify_objects_not_in_models(report, catalog_sql, catalog_models)
    verify_alembic_equals_psql(report, catalog_sql, catalog_alembic)
    verify_fk_actions(report, catalog_sql)

    print("\n" + "=" * 72)
    print(f"PASSED: {report.passed}")
    print(f"FAILED: {report.failed}")
    if report.failed:
        print("\nFAILURES BY CATEGORY:")
        for category, message in report.failures:
            print(f"  [{category}] {message}")
        print("\nRESULT: VERIFICATION FAILED")
        return 1
    print("\nRESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
