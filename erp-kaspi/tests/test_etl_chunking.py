"""P0-2: ETL chunking tests — resume semantics, no duplicates, partial failure."""

import pytest
from sqlalchemy import text


@pytest.fixture
def chunk_mapping():
    return {
        "types": {"income": "SALE_KASPI_SHOP", "expense": "EXPENSE_MATERIAL"},
        "categories": {},
        "defaults": {"comment_prefix": "Chunk test"},
    }


def _make_rows(n, start_amount=100):
    rows = []
    for i in range(n):
        rows.append({
            "type": "income" if i % 2 == 0 else "expense",
            "amount": str(start_amount + i),
            "date": "2026-01-15",
            "order_id": f"chunk-{i}",
        })
    return rows


def test_chunked_import_commits_per_chunk(engine, applied_schema, chunk_mapping):
    """Chunked import with chunk_size=10 processes 25 rows in 3 chunks."""
    import random

    from etl.import_excel import run_import

    bin_code = f"444{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('CHUNK-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

    from sqlalchemy.orm import Session
    with Session(engine) as session:
        rows = _make_rows(25)
        result = run_import(
            session, rows, chunk_mapping,
            ip_id=ip_id, batch_name="chunk-test.xlsx",
            dry_run=False, batch_comment=None,
            chunk_size=10,
        )
        # run_import manages its own transactions in chunked mode;
        # caller must commit the final batch status update
        session.commit()

    assert result["status"] == "SUCCESS"
    assert result["inserted"] == 25
    assert result["total"] == 25

    # Verify all 25 operations in DB
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM operations WHERE ip_id = :ip"),
            {"ip": ip_id},
        ).scalar_one()
    assert count == 25


def test_chunked_import_idempotent_on_retry(engine, applied_schema, chunk_mapping):
    """Re-running chunked import does not create duplicates (fingerprint)."""
    import random

    from etl.import_excel import run_import

    bin_code = f"333{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('CHUNK-RETRY', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

    from sqlalchemy.orm import Session
    rows = _make_rows(20)

    with Session(engine) as session:
        r1 = run_import(session, rows, chunk_mapping, ip_id=ip_id,
                        batch_name="retry.xlsx", dry_run=False,
                        batch_comment=None, chunk_size=10)
        session.commit()

    with Session(engine) as session:
        r2 = run_import(session, rows, chunk_mapping, ip_id=ip_id,
                        batch_name="retry.xlsx", dry_run=False,
                        batch_comment=None, chunk_size=10)
        session.commit()

    assert r1["inserted"] == 20
    assert r2["inserted"] == 0
    assert r2["duplicates"] == 20

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM operations WHERE ip_id = :ip"),
            {"ip": ip_id},
        ).scalar_one()
    assert count == 20  # No duplicates


def test_chunked_import_preserves_legacy_mode(engine, applied_schema, chunk_mapping):
    """chunk_size=None preserves single-transaction behavior."""
    import random

    from etl.import_excel import run_import

    bin_code = f"222{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('CHUNK-LEGACY', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

    from sqlalchemy.orm import Session
    with Session(engine) as session, session.begin():
        rows = _make_rows(5)
        result = run_import(session, rows, chunk_mapping, ip_id=ip_id,
                            batch_name="legacy.xlsx", dry_run=False,
                            batch_comment=None, chunk_size=None)
        # Legacy mode: caller manages transaction

    assert result["status"] == "SUCCESS"
    assert result["inserted"] == 5


# --------------------------------------------------------------------------- #
# F-ETL-004: chunk_size validation — invalid values must fail fast (TDD)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [0, -1, -100, "10", 2.5, True, False])
def test_chunk_size_invalid_raises_before_db_work(engine, applied_schema, chunk_mapping, bad):
    """F-ETL-004: chunk_size=0 crashed range(); negative silently imported
    0 rows with SUCCESS; non-integers were untested. All must raise
    ValueError BEFORE any DB work (no ImportBatch row)."""
    import random

    from sqlalchemy.orm import Session

    from etl.import_excel import run_import

    bin_code = f"555{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('CHUNK-VAL', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

    with Session(engine) as session:
        batches_before = session.execute(text("SELECT COUNT(*) FROM import_batches")).scalar_one()
        with pytest.raises(ValueError, match="chunk_size"):
            run_import(
                session, _make_rows(5), chunk_mapping,
                ip_id=ip_id, batch_name="chunk-bad.xlsx",
                dry_run=False, batch_comment=None,
                chunk_size=bad,
            )
        # Fail-fast: validation happens before the ImportBatch INSERT.
        batches_after = session.execute(text("SELECT COUNT(*) FROM import_batches")).scalar_one()
        assert batches_after == batches_before


def test_chunk_size_huge_terminates(engine, applied_schema, chunk_mapping):
    """F-ETL-004: a huge but valid chunk_size must terminate normally
    (single chunk), not hang or overflow."""
    import random

    from sqlalchemy.orm import Session

    from etl.import_excel import run_import

    bin_code = f"666{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('CHUNK-HUGE', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

    with Session(engine) as session:
        rows = _make_rows(25)
        result = run_import(
            session, rows, chunk_mapping,
            ip_id=ip_id, batch_name="chunk-huge.xlsx",
            dry_run=False, batch_comment=None,
            chunk_size=10**9,
        )
        session.commit()

    assert result["status"] == "SUCCESS"
    assert result["inserted"] == 25
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM operations WHERE ip_id = :ip"),
            {"ip": ip_id},
        ).scalar_one()
    assert count == 25
