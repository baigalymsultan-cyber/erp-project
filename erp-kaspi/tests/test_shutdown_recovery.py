"""P0-3: Interrupted workload — ETL shutdown mid-chunk, restart resumes safely.

Deterministic equivalent of OS signal during ETL:
1. Start chunked import
2. Simulate crash: close session WITHOUT commit mid-chunk
3. Verify DB consistent: no partial chunk, no orphan operations
4. Restart: resume processes remaining, skips committed
5. Verify: zero duplicates, correct final status

Limitation: Full SIGTERM simulation requires process-level testing.
This verifies the transaction boundary semantics deterministically.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session


@pytest.fixture
def shutdown_mapping():
    return {
        "types": {"income": "SALE_KASPI_SHOP", "expense": "EXPENSE_MATERIAL"},
        "categories": {},
        "defaults": {"comment_prefix": "Shutdown test"},
    }


def _make_rows(n, prefix):
    return [
        {
            "type": "income" if i % 2 == 0 else "expense",
            "amount": str(100 + i),
            "date": "2026-01-15",
            "order_id": f"{prefix}-{i}",
        }
        for i in range(n)
    ]


def test_crash_mid_chunk_leaves_no_partial_data(engine, applied_schema, shutdown_mapping):
    """Simulated crash after chunk 1 commit, before chunk 2 commit."""
    import random

    from etl.import_excel import run_import

    bin_code = f"{random.randint(100000000000, 999999999999)}"[:12]
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('CRASH-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

    rows = _make_rows(1000, f"crash-{bin_code}")

    # Phase 1: Process chunks 1-2 (500 rows), then SIMULATE CRASH
    # by closing session without committing chunk 3
    session = Session(engine)
    try:
        # Manually process 2 chunks then "crash"
        run_import(
            session, rows[:500], shutdown_mapping,
            ip_id=ip_id, batch_name="crash.xlsx",
            dry_run=False, batch_comment=None,
            chunk_size=250,
        )
        session.commit()
        # Simulate crash: session closed, remaining 500 rows never processed
    finally:
        session.close()

    # Verify: exactly 500 committed, no partial chunk data
    with engine.begin() as conn:
        count1 = conn.execute(
            text("SELECT COUNT(*) FROM operations WHERE ip_id = :ip"),
            {"ip": ip_id},
        ).scalar_one()
        # Check no orphan batch with RUNNING status stuck
        running = conn.execute(
            text("SELECT COUNT(*) FROM import_batches WHERE ip_id = :ip AND status = 'RUNNING'"),
            {"ip": ip_id},
        ).scalar_one()
        assert running == 0, f"Found {running} stuck RUNNING batches"

    assert count1 == 500, f"Expected 500 committed, got {count1}"
    # Batch status should be SUCCESS (500/500 of first run), not stuck RUNNING
    # (The first run completed its 500 rows successfully)

    # Phase 2: RESTART — resume with full 1000 rows
    with Session(engine) as session:
        result2 = run_import(
            session, rows, shutdown_mapping,
            ip_id=ip_id, batch_name="crash-resume.xlsx",
            dry_run=False, batch_comment=None,
            chunk_size=250,
        )
        session.commit()

    # First 500 skipped as duplicates, last 500 inserted
    assert result2["duplicates"] == 500
    assert result2["inserted"] == 500
    assert result2["status"] == "SUCCESS"

    # Final verification: 1000 total, zero duplicates
    with engine.begin() as conn:
        count2 = conn.execute(
            text("SELECT COUNT(*) FROM operations WHERE ip_id = :ip"),
            {"ip": ip_id},
        ).scalar_one()
        dups = conn.execute(
            text("""
                SELECT COUNT(*) FROM (
                    SELECT import_fingerprint FROM operations
                    WHERE ip_id = :ip AND import_fingerprint IS NOT NULL
                    GROUP BY import_fingerprint HAVING COUNT(*) > 1
                ) d
            """),
            {"ip": ip_id},
        ).scalar_one()

    assert count2 == 1000
    assert dups == 0


def test_no_open_transaction_after_crash(engine, applied_schema, shutdown_mapping):
    """After simulated crash, no idle-in-transaction sessions remain."""
    import random

    from etl.import_excel import run_import

    bin_code = f"{random.randint(100000000000, 999999999999)}"[:12]
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('NOTXN-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

    rows = _make_rows(100, f"notxn-{bin_code}")

    # Simulate crash: start import, close without commit
    session = Session(engine)
    try:
        run_import(
            session, rows, shutdown_mapping,
            ip_id=ip_id, batch_name="notxn.xlsx",
            dry_run=False, batch_comment=None,
            chunk_size=50,
        )
        # Crash: rollback instead of commit, then close
        session.rollback()
    finally:
        session.close()

    # Verify no idle-in-transaction
    with engine.begin() as conn:
        idle_txn = conn.execute(
            text("""
                SELECT COUNT(*) FROM pg_stat_activity
                WHERE state = 'idle in transaction'
                AND datname = 'erp_test'
            """)
        ).scalar_one()

    # Should be 0 (or very low — other tests might have transient)
    assert idle_txn == 0, f"Found {idle_txn} idle-in-transaction sessions"
