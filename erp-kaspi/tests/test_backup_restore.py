"""§73 — restore test: a backup counts only if it restores.

Creates a fresh backup of the dev database via scripts/backup.sh, restores
it into a scratch database via scripts/restore.sh, and compares table row
counts between source and restored copies. The scratch database is dropped
afterwards. Skipped when PostgreSQL is unreachable or the app user cannot
create databases.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

DB_HOST = os.environ.get("DATABASE_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DATABASE_PORT", "5432")
DB_USER = os.environ.get("DATABASE_USER", "erp_app")
DB_PASSWORD = os.environ.get("TEST_DATABASE_PASSWORD", "devpass_local_only")
SOURCE_DB = "erp_dev"
SCRATCH_DB = "erp_restore_test"

#: Business tables whose contents must survive the backup/restore round trip.
CHECKED_TABLES = (
    "users",
    "roles",
    "user_roles",
    "counterparties",
    "employees",
    "operations",
    "order_materials",
    "transfers",
    "kaspi_payouts",
    "orders",
    "order_items",
    "rates_history",
    "work_entries",
)

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _psql(db: str, sql: str) -> str:
    env = {**os.environ, "PGPASSWORD": DB_PASSWORD}
    return subprocess.run(
        ["psql", "-h", DB_HOST, "-p", DB_PORT, "-U", DB_USER, "-d", db, "-tAc", sql],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout.strip()


def _db_exists(name: str) -> bool:
    return _psql("postgres", f"SELECT 1 FROM pg_database WHERE datname='{name}'") == "1"


def _row_counts(db: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in CHECKED_TABLES:
        raw = _psql(db, f"SELECT count(*) FROM {table}")
        counts[table] = int(raw or "0")
    return counts


@pytest.fixture()
def scratch_db() -> Iterator[str]:
    if not DB_PASSWORD:
        pytest.skip("TEST_DATABASE_PASSWORD is not set")
    try:
        if _db_exists(SCRATCH_DB):
            subprocess.run(
                ["dropdb", "-h", DB_HOST, "-p", DB_PORT, "-U", DB_USER, "--if-exists", SCRATCH_DB],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "PGPASSWORD": DB_PASSWORD},
            )
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"PostgreSQL is not reachable for the restore test: {exc.stderr}")
    yield SCRATCH_DB
    subprocess.run(
        ["dropdb", "-h", DB_HOST, "-p", DB_PORT, "-U", DB_USER, "--if-exists", SCRATCH_DB],
        capture_output=True,
        text=True,
        env={**os.environ, "PGPASSWORD": DB_PASSWORD},
    )


def test_backup_restores_with_matching_rows(scratch_db: str) -> None:
    """§73 — backup.sh output restores into a scratch DB with equal rows."""
    env = {**os.environ, "DB_PASSWORD": DB_PASSWORD, "DB_NAME": SOURCE_DB}
    backup = subprocess.run(
        ["bash", "scripts/backup.sh"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    backup_lines = [ln for ln in backup.stdout.strip().splitlines() if ln.startswith("BACKUP OK:")]
    assert backup_lines, backup.stdout + backup.stderr
    # The path is everything after "BACKUP OK: " - it may contain spaces
    # (e.g. a workspace folder literally named "All files"), so never split().
    dump_path = Path(backup_lines[-1].split("BACKUP OK: ", 1)[1].strip())
    assert dump_path.exists() and dump_path.stat().st_size > 0, backup_lines[-1]

    restore = subprocess.run(
        ["bash", "scripts/restore.sh", str(dump_path)],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        env={**os.environ, "DB_PASSWORD": DB_PASSWORD, "TARGET_DB": scratch_db},
        check=True,
    )
    assert "RESTORE OK" in restore.stdout, restore.stderr

    source_counts = _row_counts(SOURCE_DB)
    restored_counts = _row_counts(scratch_db)
    for table in CHECKED_TABLES:
        assert restored_counts[table] == source_counts[table], (
            f"{table}: source={source_counts[table]} restored={restored_counts[table]}"
        )
