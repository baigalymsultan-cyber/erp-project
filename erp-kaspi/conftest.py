"""
Shared pytest fixtures.

MASTER PROMPT §58, §68: tests run against a real PostgreSQL instance, because
the guarantees under test (triggers, EXCLUDE constraints, GENERATED columns,
partial unique indexes) do not exist in SQLite and cannot be emulated.

The target database is taken from the environment (§10). Defaults point at the
local sandbox database created by scripts/apply_schema.sh.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, bindparam, inspect, text
from sqlalchemy.orm import Session

from app.core.config import DatabaseSettings
from app.core.database import create_database_engine

#: Environment used by the test session; TEST_DATABASE_* overrides DATABASE_*.
_TEST_ENV_PREFIX = "TEST_DATABASE_"


def pytest_configure(config: pytest.Config) -> None:
    """Propagate TEST_DATABASE_* into DATABASE_* before app modules import.

    The FastAPI application builds its engine at import time and reads the
    plain DATABASE_* names. Exporting the overrides here guarantees the app,
    the ETL scripts and the fixture engine all talk to the same database.
    """
    for key, value in list(os.environ.items()):
        if key.startswith(_TEST_ENV_PREFIX):
            target = "DATABASE_" + key[len(_TEST_ENV_PREFIX) :]
            os.environ.setdefault(target, value)
    # The application layer defaults to erp_dev; tests must never touch it.
    # Point DATABASE_* at erp_test unless the caller already did so.
    os.environ.setdefault("DATABASE_NAME", "erp_test")
    os.environ.setdefault("DATABASE_USER", "erp_app")
    os.environ.setdefault("DATABASE_HOST", "127.0.0.1")
    os.environ.setdefault("DATABASE_PORT", "5432")
    # The local role erp_app is created with this password by
    # scripts/apply_schema.sh (§10 local-only dev credential). Without it the
    # app engine, which is built at import time, would attempt passwordless
    # auth and fail with fe_sendauth before the fixture-level defaults apply.
    os.environ.setdefault("DATABASE_PASSWORD", "devpass_local_only")
    # §10: the auth endpoints need a signing secret; tests supply a fixed
    # local-only value unless the caller already provided one.
    os.environ.setdefault("APP_JWT_SECRET", "test-only-secret-0123456789abcdef-0123456789abcdef")


def _test_settings() -> DatabaseSettings:
    """Build settings, preferring TEST_DATABASE_* over DATABASE_* (§10)."""
    env = dict(os.environ)
    for key, value in list(env.items()):
        if key.startswith(_TEST_ENV_PREFIX):
            env["DATABASE_" + key[len(_TEST_ENV_PREFIX) :]] = value
    env.setdefault("DATABASE_PASSWORD", "devpass_local_only")
    env.setdefault("DATABASE_NAME", "erp_test")
    env.setdefault("DATABASE_USER", "erp_app")
    env.setdefault("DATABASE_HOST", "127.0.0.1")
    env.setdefault("DATABASE_PORT", "5432")
    return DatabaseSettings.from_env(env)


@pytest.fixture(scope="session")
def db_settings() -> DatabaseSettings:
    return _test_settings()


@pytest.fixture(scope="session")
def engine(db_settings: DatabaseSettings) -> Iterator[Engine]:
    """Engine bound to the test database; skips the suite when unreachable."""
    eng = create_database_engine(db_settings)
    try:
        with eng.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        eng.dispose()
        pytest.skip(
            "PostgreSQL test database is not reachable at "
            f"{db_settings.safe_dsn()}: {type(exc).__name__}: {exc}"
        )
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def applied_schema(engine: Engine) -> None:
    """Fail fast when the target database does not contain the STEP 1 schema."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "ip_entities" not in tables or "operations" not in tables:
        pytest.skip(
            "The test database does not contain the ERP schema. "
            "Run: DB_NAME=erp_test scripts/apply_schema.sh --recreate"
        )


@pytest.fixture
def rollback_session(engine: Engine) -> Iterator[Session]:
    """A raw Session inside one transaction, always rolled back (TD-012)."""
    session = Session(bind=engine, future=True)
    session.begin()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def session(engine: Engine, applied_schema: None) -> Iterator[Session]:
    """
    Session wrapped in a transaction that is always rolled back.

    No test may leave data behind, so every fixture-provided session runs
    inside an explicit transaction that is discarded at teardown.
    """
    connection = engine.connect()
    transaction = connection.begin()
    db_session = Session(bind=connection, expire_on_commit=False, future=True)
    try:
        yield db_session
    finally:
        db_session.close()
        # A flush that failed under pytest.raises() leaves the root
        # transaction deassociated; roll back only when it is still usable.
        if transaction.is_active:
            transaction.rollback()
        connection.close()


# --------------------------------------------------------------------------- #
# §94 — the suite leaves no invented business data behind
# --------------------------------------------------------------------------- #

#: Business tables purged at session end, children first (FK-safe order).
#: `operation_types`, `categories`, `materials`, `products`, `counterparties`,
#: `accounts`, `rates_history`, `users`, `roles`, `user_roles`,
#: `work_operation_types`, `audit_log` are reference/system rows seeded by the
#: schema and are preserved; rows created by tests in the tables below are
#: business data and must not survive the run (§94).
#: TD-019 (2026-10-01): provenance parents are purged AFTER their children.
#: operations/transfers/kaspi_payouts reference raw_kaspi_payloads and
#: import_batches (ON DELETE SET NULL); deleting the parents first would
#: silently null out the provenance links. raw_kaspi_payloads in turn
#: references sync_log, so sync_log goes after it.
_BUSINESS_TABLES_PURGE_ORDER: tuple[str, ...] = (
    "bank_statement_lines",  # G-BANK-3: before operations/accounts/statements
    "raw_bank_statements",  # G-BANK-2: before import_batches/accounts
    "kaspi_payout_items",
    "kaspi_payouts",
    "kaspi_api_credentials",
    "tax_rates_history",
    "payroll_payments",
    "payroll_summary",
    "work_entries",
    "payroll_periods",
    "employees",
    "transfers",
    "material_reservations",  # P2-INV-003: before orders/materials
    "production_output",  # G-C2: before orders/products
    "stock_movements",  # P2-INV-001: immutable ledger, before settlement_links
    "settlement_links",
    "operations",
    "accounts",
    "order_materials",
    "order_status_history",
    "order_items",
    "orders",
    "raw_kaspi_payloads",
    "import_batches",
    "sync_log",
    "ip_entities",
)


def _purge_business_rows(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.allow_financial_delete = 'on'"))
        for table in _BUSINESS_TABLES_PURGE_ORDER:
            conn.execute(text(f"DELETE FROM {table}"))


def _purge_reference_rows(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM rates_history WHERE work_operation_type_id IN "
                "(SELECT id FROM work_operation_types WHERE code NOT IN :seeded)"
            ).bindparams(bindparam("seeded", expanding=True)),
            {"seeded": _SEEDED_WORK_TYPE_CODES},
        )
        conn.execute(
            text("DELETE FROM work_operation_types WHERE code NOT IN :seeded").bindparams(
                bindparam("seeded", expanding=True)
            ),
            {"seeded": _SEEDED_WORK_TYPE_CODES},
        )
        conn.execute(text("DELETE FROM user_roles"))
        conn.execute(text("DELETE FROM users"))


def _migrator_settings() -> DatabaseSettings | None:
    """Settings for a schema-owner (migrator) connection, or None.

    TD-003: the §16 escape hatch app.allow_financial_delete is gated on
    membership in the migrator role, so the session-end purge must run as
    that role. The password comes from TEST_DATABASE_MIGRATOR_PASSWORD
    (preferred) or DB_MIGRATOR_PASSWORD (as set in CI). No default: without
    an explicit migrator password the purge is skipped with a warning.
    """
    env = dict(os.environ)
    password = env.get("TEST_DATABASE_MIGRATOR_PASSWORD") or env.get("DB_MIGRATOR_PASSWORD")
    if not password:
        return None
    for key, value in list(env.items()):
        if key.startswith(_TEST_ENV_PREFIX):
            env["DATABASE_" + key[len(_TEST_ENV_PREFIX) :]] = value
    env.setdefault("DATABASE_PASSWORD", password)
    env["DATABASE_PASSWORD"] = password
    env["DATABASE_USER"] = env.get("TEST_DATABASE_MIGRATOR_USER") or env.get(
        "DB_MIGRATOR_USER", "erp_migrator"
    )
    env.setdefault("DATABASE_NAME", "erp_test")
    return DatabaseSettings.from_env(env)


@pytest.fixture(scope="session", autouse=True)
def _purge_business_rows_at_session_end(
    request,
    _purge_test_created_reference_rows,
) -> Iterator[None]:
    """§94: after the last test, remove business rows created by the suite.

    Tests that need committed rows (the FastAPI suite commits per request,
    §16) register here instead of cleaning up one by one: at session end the
    business tables are truncated in FK-safe order, so a §94 audit after the
    run sees a freshly provisioned database.
    """
    request.getfixturevalue("engine")  # ensure the suite really ran against PostgreSQL
    migrator = _migrator_settings()
    if migrator is None:
        pytest.fail(
            "§94 test isolation requires TEST_DATABASE_MIGRATOR_PASSWORD or "
            "DB_MIGRATOR_PASSWORD; refusing to run tests with cleanup disabled"
        )

    validation_engine = create_database_engine(migrator)
    try:
        with validation_engine.connect() as conn:
            current_user = conn.execute(text("SELECT current_user")).scalar_one()
            if current_user != migrator.user:
                pytest.fail(
                    "§94 cleanup must connect as the configured migrator role; "
                    f"connected as {current_user!r}"
                )
    except Exception as exc:
        pytest.fail(
            "§94 test isolation cannot validate the migrator connection: "
            f"{type(exc).__name__}"
        )
    finally:
        validation_engine.dispose()

    def purge_all_rows() -> None:
        purge_engine = create_database_engine(migrator)
        try:
            _purge_business_rows(purge_engine)
            _purge_reference_rows(purge_engine)
        finally:
            purge_engine.dispose()

    # A prior SIGKILL can prevent the previous session finalizer from running.
    # Establish the documented empty-test-database contract before collecting
    # tests, then repeat the purge after the suite for the next run.
    purge_all_rows()
    yield
    purge_all_rows()


#: The 11 work operation types seeded by database/schema.sql (§30). Must stay
#: in sync with REQUIRED_WORK_TYPES in scripts/audit_step1.py.
_SEEDED_WORK_TYPE_CODES: tuple[str, ...] = (
    "SAWING",
    "EDGING_STATIONARY",
    "EDGING_MANUAL",
    "DRILLING_FITTING",
    "MILLING",
    "DRILLING_CONFIRMAT",
    "ASSEMBLY",
    "PACKAGING",
    "LOADING",
    "DELIVERY_MOUNT",
    "MATERIAL_RECEIVING",
)


@pytest.fixture(scope="session", autouse=True)
def _purge_test_created_reference_rows(request) -> Iterator[None]:
    """Provide the reference-data fixture dependency for the session purge.

    The parent session fixture owns teardown ordering: it removes business
    children such as `work_entries` before deleting test-created reference
    rows such as `work_operation_types`. Keeping this fixture teardown-free
    prevents pytest from running the reference purge first.
    """
    request.getfixturevalue("engine")
    yield


#: Bootstrap credentials for API tests (§53). The suite logs in once per
#: session and reuses the Bearer token: /auth/login is rate-limited to
#: 5 attempts / 60 s per client, so per-test logins would trip the limiter.
_SUITE_ADMIN_USER = "erp-suite-admin"
_SUITE_ADMIN_PASSWORD = "Suite-Admin-2026!"


@pytest.fixture(scope="session")
def admin_headers(engine: Engine, applied_schema) -> dict[str, str]:
    """Bearer headers of an ADMIN principal for API tests (§53)."""
    from fastapi.testclient import TestClient

    from app.api import app
    from app.core.security import hash_password

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash) "
                "VALUES (:u, :h) ON CONFLICT (username) DO NOTHING"
            ),
            {"u": _SUITE_ADMIN_USER, "h": hash_password(_SUITE_ADMIN_PASSWORD)},
        )
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT u.id, r.id FROM users u, roles r "
                "WHERE u.username = :u AND r.code = 'ADMIN' "
                "ON CONFLICT DO NOTHING"
            ),
            {"u": _SUITE_ADMIN_USER},
        )
    with TestClient(app) as client:
        response = client.post(
            "/auth/login",
            json={"username": _SUITE_ADMIN_USER, "password": _SUITE_ADMIN_PASSWORD},
        )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
