"""
Tests for the Alembic baseline revision (0001_baseline_schema).

MASTER PROMPT references
------------------------
§7   — Alembic is the migration tool; the migration must actually work.
§16  — financial data is never destroyed by a routine operation, therefore
       downgrade() must refuse to run without an explicit opt-in.
§84  — the migration must produce the full schema, not a subset.
§87  — the migration must be re-runnable, including on top of a database that
       was already provisioned by scripts/apply_schema.sh.
§89  — no placeholders: the docstring of the revision references this file, so
       this file must exist and must actually assert the claims made there.
§90  — every guarantee is verified by an automated check, not by inspection.

ADR-011 — database/schema.sql is the source of truth; the revision executes it
verbatim rather than re-declaring 2 000 lines of DDL in Python.

Scope and limits (§93)
----------------------
The heavyweight end-to-end proof (create an empty database, run
`alembic upgrade head`, run the 623-check STEP 1 audit against the result,
downgrade, upgrade again) requires CREATE DATABASE, which the application role
erp_app deliberately does not have (rolcreatedb=f, §11). That scenario is
executed manually and its result is recorded in docs/PROGRESS.md; the tests
below verify everything that can be asserted without superuser rights:

  * the revision graph (single head, correct identifiers);
  * the loader that makes the SQL driver-executable (BUG-002);
  * that the applied database matches the revision's own object inventory;
  * that downgrade() refuses to run without ERP_ALLOW_BASELINE_DOWNGRADE=1.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
REVISION_FILE = PROJECT_ROOT / "alembic" / "versions" / "20260828_0000_0001_baseline_schema.py"
SCHEMA_FILE = PROJECT_ROOT / "database" / "schema.sql"

#: The only psql client meta-command that schema.sql is allowed to contain.
#: If a new one is added, the revision must be taught about it explicitly
#: rather than silently skipping it (§1).
EXPECTED_META_COMMANDS = [r"\set ON_ERROR_STOP on"]


def _load_revision_module() -> ModuleType:
    """
    Import the revision file as a standalone module.

    Alembic revision files are not importable as package members (the versions
    directory has no __init__.py by design), so the module is loaded from its
    path. This is the same mechanism Alembic itself uses.
    """
    spec = importlib.util.spec_from_file_location("erp_baseline_revision", REVISION_FILE)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load revision module from {REVISION_FILE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def revision_module() -> ModuleType:
    return _load_revision_module()


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


# ---------------------------------------------------------------------------
# 1. Revision graph
# ---------------------------------------------------------------------------


def test_revision_file_exists() -> None:
    assert REVISION_FILE.is_file(), f"missing revision file: {REVISION_FILE}"


def test_single_head(script_directory: ScriptDirectory) -> None:
    """A branched migration history is a defect, not a feature."""
    heads = script_directory.get_heads()
    assert heads == ["0023_production_output"], f"unexpected heads: {heads}"


def test_baseline_is_the_base_revision(
    script_directory: ScriptDirectory,
) -> None:
    revision = script_directory.get_revision("0001_baseline_schema")
    assert revision.down_revision is None, (
        "the baseline revision must be the root of the history; "
        f"down_revision={revision.down_revision!r}"
    )


def test_revision_identifiers(revision_module: ModuleType) -> None:
    assert revision_module.revision == "0001_baseline_schema"
    assert revision_module.down_revision is None
    assert revision_module.branch_labels is None
    assert revision_module.depends_on is None


def test_revision_points_at_the_schema_file(revision_module: ModuleType) -> None:
    assert revision_module.SCHEMA_FILE == SCHEMA_FILE
    assert revision_module.SCHEMA_FILE.is_file()


# ---------------------------------------------------------------------------
# 2. BUG-002 — psql meta-commands must be stripped, and only those
# ---------------------------------------------------------------------------


def test_schema_file_still_contains_the_psql_guard() -> None:
    """
    The `\\set ON_ERROR_STOP on` directive stays in schema.sql.

    It is what makes `psql -f database/schema.sql` abort on the first error.
    Deleting it to make the driver path work would weaken the psql path, which
    §1 forbids; the driver path strips it instead.
    """
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    assert r"\set ON_ERROR_STOP on" in sql


def test_only_known_meta_commands_are_stripped(
    revision_module: ModuleType,
) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    _, stripped = revision_module._strip_psql_meta_commands(sql)
    assert stripped == EXPECTED_META_COMMANDS, (
        "schema.sql contains psql meta-commands that the baseline revision "
        f"does not know about: {sorted(set(stripped) - set(EXPECTED_META_COMMANDS))}"
    )


def test_stripped_sql_has_no_leading_backslash_lines(
    revision_module: ModuleType,
) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    executable, _ = revision_module._strip_psql_meta_commands(sql)
    offenders = [
        (number, line)
        for number, line in enumerate(executable.splitlines(), start=1)
        if line.lstrip().startswith("\\")
    ]
    assert not offenders, f"driver-executable SQL still has psql commands: {offenders}"


def test_stripping_preserves_every_ddl_statement(
    revision_module: ModuleType,
) -> None:
    """
    Stripping must remove client directives only — never DDL.

    Counting the CREATE statements before and after guards against a regex
    that is too greedy.
    """
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    executable, _ = revision_module._strip_psql_meta_commands(sql)
    for keyword in (
        "CREATE TABLE IF NOT EXISTS",
        "CREATE OR REPLACE FUNCTION",
        "CREATE OR REPLACE VIEW",
        "CREATE TRIGGER",
        "CREATE INDEX IF NOT EXISTS",
        "CREATE UNIQUE INDEX IF NOT EXISTS",
        "ALTER TABLE",
        "INSERT INTO",
    ):
        assert sql.count(keyword) == executable.count(keyword), (
            f"stripping changed the number of {keyword!r} statements: "
            f"{sql.count(keyword)} -> {executable.count(keyword)}"
        )


def test_stripped_sql_length_difference_is_exactly_the_directives(
    revision_module: ModuleType,
) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    executable, stripped = revision_module._strip_psql_meta_commands(sql)
    removed = len(sql) - len(executable)
    expected = sum(len(line) for line in stripped)
    assert removed == expected, (
        f"removed {removed} characters but the stripped directives account for only {expected}"
    )


# ---------------------------------------------------------------------------
# 3. The revision's object inventory must match the real database
# ---------------------------------------------------------------------------


def test_downgrade_table_list_matches_database(
    revision_module: ModuleType, engine: Engine, applied_schema: None
) -> None:
    """
    downgrade() must know about every table, otherwise it leaves orphans.

    alembic_version is excluded: it is Alembic's own bookkeeping table and is
    managed by Alembic, not by the revision body.
    """
    inspector = inspect(engine)
    database_tables = set(inspector.get_table_names()) - {"alembic_version"}
    revision_tables = set(revision_module._TABLES)

    # Tables added by later revisions in the chain (0004 auth_rate_events,
    # TD-015) have their own downgrade that drops them; the baseline
    # downgrade must not (and does not) drop them itself.
    database_tables -= {"auth_rate_events"}

    assert revision_tables == database_tables, (
        "downgrade() would not drop: "
        f"{sorted(database_tables - revision_tables)}; "
        "downgrade() lists tables that do not exist: "
        f"{sorted(revision_tables - database_tables)}"
    )


def test_downgrade_table_list_has_no_duplicates(
    revision_module: ModuleType,
) -> None:
    tables = list(revision_module._TABLES)
    assert len(tables) == len(set(tables)), "duplicate entries in _TABLES"


def test_downgrade_view_list_matches_database(
    revision_module: ModuleType, engine: Engine, applied_schema: None
) -> None:
    inspector = inspect(engine)
    database_views = set(inspector.get_view_names())
    revision_views = set(revision_module._VIEWS)
    assert revision_views == database_views, (
        f"views only in database: {sorted(database_views - revision_views)}; "
        f"views only in downgrade(): {sorted(revision_views - database_views)}"
    )


def test_downgrade_function_list_matches_database(
    revision_module: ModuleType, engine: Engine, applied_schema: None
) -> None:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT p.proname
                  FROM pg_proc p
                  JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = 'public'
                   AND p.proname LIKE 'fn\\_%'
                """
            )
        ).all()
    database_functions = {row[0] for row in rows}
    revision_functions = {name.split("(", 1)[0] for name in revision_module._FUNCTIONS}
    assert revision_functions == database_functions, (
        "functions only in database: "
        f"{sorted(database_functions - revision_functions)}; "
        "functions only in downgrade(): "
        f"{sorted(revision_functions - database_functions)}"
    )


def test_downgrade_lists_dependent_tables_before_their_parents(
    revision_module: ModuleType,
) -> None:
    """
    _TABLES is documented as being in reverse dependency order.

    CASCADE makes the order non-fatal, but a wrong order would hide real
    dependency mistakes, so the documented property is asserted on the two
    unambiguous non-circular chains.
    """
    order = {name: index for index, name in enumerate(revision_module._TABLES)}
    chains = (
        ("payroll_payments", "payroll_summary"),
        ("payroll_summary", "work_entries"),
        ("work_entries", "employees"),
        ("rates_history", "work_operation_types"),
        ("order_items", "orders"),
        ("order_materials", "materials"),
        ("kaspi_payout_items", "kaspi_payouts"),
        ("user_roles", "users"),
        ("user_roles", "roles"),
        ("transfers", "ip_entities"),
        ("operations", "operation_types"),
    )
    for child, parent in chains:
        assert order[child] < order[parent], (
            f"{child} must be dropped before {parent}, but appears after it"
        )


# ---------------------------------------------------------------------------
# 4. §16 — downgrade refuses to destroy the ledger without an opt-in
# ---------------------------------------------------------------------------


def test_downgrade_refuses_without_opt_in(
    revision_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERP_ALLOW_BASELINE_DOWNGRADE", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        revision_module.downgrade()
    message = str(excinfo.value)
    assert "ERP_ALLOW_BASELINE_DOWNGRADE" in message
    assert "§16" in message


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "TRUE", "2"])
def test_downgrade_refuses_for_non_exact_values(
    revision_module: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Only the exact string "1" enables the destructive path."""
    monkeypatch.setenv("ERP_ALLOW_BASELINE_DOWNGRADE", value)
    with pytest.raises(RuntimeError):
        revision_module.downgrade()


def test_downgrade_does_not_drop_btree_gist(revision_module: ModuleType) -> None:
    """
    btree_gist is intentionally kept: it may be shared with other schemas.

    Asserted on the source text because executing downgrade() here would
    destroy the developer's database.
    """
    source = REVISION_FILE.read_text(encoding="utf-8")
    assert "DROP EXTENSION" not in source


# ---------------------------------------------------------------------------
# 5. upgrade() guards
# ---------------------------------------------------------------------------


def test_upgrade_rejects_a_missing_schema_file(
    revision_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(revision_module, "SCHEMA_FILE", tmp_path / "does_not_exist.sql")
    with pytest.raises(RuntimeError, match=r"schema\.sql not found"):
        revision_module.upgrade()


def test_upgrade_rejects_a_foreign_sql_file(
    revision_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A wrong file must be rejected instead of half-applied.

    The marker check is what stops `alembic upgrade head` from executing an
    unrelated SQL script that happens to sit at that path.
    """
    foreign = tmp_path / "schema.sql"
    foreign.write_text("CREATE TABLE something_else (id int);\n", encoding="utf-8")
    monkeypatch.setattr(revision_module, "SCHEMA_FILE", foreign)
    with pytest.raises(RuntimeError, match="marker table ip_entities"):
        revision_module.upgrade()


def test_upgrade_rejects_unknown_meta_commands(
    revision_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    An unknown psql directive aborts the migration.

    Silently skipping a directive such as `\\i other_file.sql` would omit part
    of the schema — precisely the omission §1 forbids.
    """
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    doctored = tmp_path / "schema.sql"
    doctored.write_text(sql + "\n\\i extra_objects.sql\n", encoding="utf-8")
    monkeypatch.setattr(revision_module, "SCHEMA_FILE", doctored)

    executed: list[str] = []
    monkeypatch.setattr(
        revision_module.op,
        "execute",
        lambda statement, *args: executed.append(str(statement)),
    )

    with pytest.raises(RuntimeError, match="psql meta-commands"):
        revision_module.upgrade()
    assert not executed, "the migration must abort before executing any SQL"


def test_upgrade_executes_the_stripped_schema(
    revision_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    upgrade() passes the whole schema to the database in one call.

    op.execute is replaced so that the assertion does not touch a database:
    the applied-schema equivalence is proven separately by
    tests/test_models_match_schema.py and by scripts/audit_step1.py.
    """
    executed: list[Any] = []
    monkeypatch.setattr(
        revision_module.op,
        "execute",
        lambda statement, *args: executed.append(str(statement)),
    )

    revision_module.upgrade()

    assert len(executed) == 1, f"expected one execute() call, got {len(executed)}"
    statement = executed[0]
    assert "CREATE TABLE IF NOT EXISTS ip_entities" in statement
    assert "CREATE OR REPLACE VIEW v_ip_financial_summary" in statement
    assert r"\set" not in statement


# ---------------------------------------------------------------------------
# 6. Configuration hygiene (§10)
# ---------------------------------------------------------------------------


def test_alembic_ini_stores_no_credentials() -> None:
    """The DSN never lives in a tracked file (§10)."""
    content = ALEMBIC_INI.read_text(encoding="utf-8")
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(";") or not stripped:
            continue
        assert not stripped.startswith("sqlalchemy.url"), (
            "alembic.ini must not define sqlalchemy.url; the URL is built "
            "from the environment by alembic/env.py"
        )
    assert "devpass" not in content
    assert "postgresql://" not in content


def test_alembic_ini_runs_in_utc() -> None:
    """§15 — timestamps are UTC everywhere, including migration filenames."""
    content = ALEMBIC_INI.read_text(encoding="utf-8")
    assert "timezone = UTC" in content


def test_env_py_disables_autogenerate_comparisons() -> None:
    """
    ADR-011 — autogenerate is off; the type/default comparators stay disabled.

    If someone re-enables them, `alembic revision --autogenerate` starts
    emitting destructive diffs against the hand-written schema.
    """
    content = (PROJECT_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    assert content.count("compare_type=False") == 2
    assert content.count("compare_server_default=False") == 2


def test_revision_file_has_no_placeholders() -> None:
    """§89 — no TODO / FIXME / stub markers in delivered code."""
    content = REVISION_FILE.read_text(encoding="utf-8")
    for marker in ("TODO", "FIXME", "XXX", "placeholder", "не реализовано"):
        assert marker not in content, f"placeholder marker {marker!r} in revision"


def test_environment_is_not_polluted_by_the_tests() -> None:
    """The destructive opt-in must never leak out of a test into a real run."""
    assert os.environ.get("ERP_ALLOW_BASELINE_DOWNGRADE") is None
