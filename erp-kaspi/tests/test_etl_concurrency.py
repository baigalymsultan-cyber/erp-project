"""F-ETL-008 regression guard: TRUE concurrent ETL import.

The sequential suite (test_etl.py same-file-twice, test_etl_production.py chunk
restart) cannot catch a regression of the per-row flush + IntegrityError ->
duplicate logic: two concurrent transactions importing the same rows would
double-book if the savepoint ordering regresses. This test runs two threads
with separate sessions against the same 500 rows and pins the outcome.
"""

import random
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.orm import Session

from etl.import_excel import run_import


def _mapping():
    return {
        "types": {"income": "SALE_KASPI_SHOP", "expense": "EXPENSE_MATERIAL"},
        "categories": {},
        "defaults": {"comment_prefix": "Concurrency test"},
    }


def _make_rows(n):
    rows = []
    for i in range(n):
        rows.append(
            {
                "type": "income" if i % 2 == 0 else "expense",
                "amount": str(100 + i),
                "date": "2026-01-15",
                "order_id": f"race-{i}",
            }
        )
    return rows


def test_concurrent_same_rows_import_once(engine, applied_schema):
    """Two threads, separate sessions, same 500 rows.

    Exactly 500 operations must exist afterwards (no double-book), the loser
    must report duplicates (not errors), and no IntegrityError may escape
    run_import to the caller.
    """
    bin_code = f"777{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('ETL-RACE', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

    rows = _make_rows(500)
    barrier = threading.Barrier(2)
    outcomes = {}

    def _worker(name):
        try:
            with Session(engine) as session:
                barrier.wait(timeout=60)  # both threads start the import together
                result = run_import(
                    session,
                    rows,
                    _mapping(),
                    ip_id=ip_id,
                    batch_name=f"race-{name}.xlsx",
                    dry_run=False,
                    batch_comment=None,
                    chunk_size=None,
                )
                session.commit()
                outcomes[name] = ("ok", result)
        except Exception as exc:
            outcomes[name] = ("error", f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_worker, ("t1", "t2")))

    assert set(outcomes) == {"t1", "t2"}, outcomes
    for name, (status, payload) in outcomes.items():
        assert status == "ok", f"{name} raised {payload}"

    r1 = outcomes["t1"][1]
    r2 = outcomes["t2"][1]
    assert r1["status"] == "SUCCESS", r1
    assert r2["status"] == "SUCCESS", r2
    # Exactly one copy of each row across both imports.
    assert r1["inserted"] + r2["inserted"] == 500
    # The loser saw the winner's rows as duplicates (via the F-ETL-008
    # per-row flush + IntegrityError path), not as errors.
    assert r1["duplicates"] + r2["duplicates"] == 500
    assert r1["errors"] == 0 and r2["errors"] == 0

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM operations WHERE ip_id = :ip"),
            {"ip": ip_id},
        ).scalar_one()
    assert count == 500
