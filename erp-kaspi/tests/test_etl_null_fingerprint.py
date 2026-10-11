"""NULL-fingerprint re-import behavior (TEST_GAPS row 14).

uq_operations_import_fingerprint is partial (WHERE import_fingerprint
IS NOT NULL): rows without a fingerprint are invisible to the fingerprint
index. This module pins the behavior and guards the ETL invariant.

DECISION (documented, 2026-10-04):
- The ETL path (run_import) ALWAYS generates a fingerprint via
  row_fingerprint — there is no code path that inserts an ETL row with
  NULL import_fingerprint. A defensive guard in _process_row enforces
  this (fail the row, never insert fingerprint-less).
- Manual operations (POST /operations without Idempotency-Key) and legacy
  rows keep import_fingerprint=NULL — unchanged by design. When the ETL
  meets such a row on the same (ip_id, type_id, external_id):
    * identical content (recomputed canonical fingerprint matches) ->
      counted as duplicate, no double-book;
    * different content -> loud row error, never a silent double-book
      and never a silent skip.
"""

import random

from sqlalchemy import text
from sqlalchemy.orm import Session

from etl.import_excel import run_import


def _mapping():
    return {
        "types": {"income": "SALE_KASPI_SHOP"},
        "categories": {},
        "defaults": {"comment_prefix": "NullFP test"},
    }


def _seed_manual_null_fp(engine, bin_prefix, name, amount="500"):
    """Manual/legacy row with import_fingerprint=NULL (as POST /operations
    without Idempotency-Key leaves it). Returns (ip_id, type_id)."""
    bin_code = f"{bin_prefix}{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": name, "b": bin_code},
        ).scalar_one()
        type_id = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_SHOP'")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, "
                "accrual_date, external_id, status, import_fingerprint) "
                "VALUES (:ip, :t, :a, '2026-03-10', '2026-03-10', 'MANUAL-1', 'POSTED', NULL)"
            ),
            {"ip": ip_id, "t": type_id, "a": amount},
        )
    return ip_id


def _op_count(engine, ip_id):
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM operations WHERE ip_id = :ip"),
            {"ip": ip_id},
        ).scalar_one()


def test_null_fingerprint_identical_content_is_duplicate(engine, applied_schema):
    """A manual NULL-fingerprint row with content identical to the incoming
    ETL row is treated as duplicate: no double-book, no error."""
    ip_id = _seed_manual_null_fp(engine, "414", "NULLFP")

    rows = [{"type": "income", "amount": "500", "date": "2026-03-10", "order_id": "MANUAL-1"}]
    with Session(engine) as session:
        result = run_import(
            session,
            rows,
            _mapping(),
            ip_id=ip_id,
            batch_name="nullfp.xlsx",
            dry_run=False,
            batch_comment=None,
            chunk_size=None,
        )
        session.commit()

    assert result["inserted"] == 0
    assert result["duplicates"] == 1
    assert result["errors"] == 0
    assert _op_count(engine, ip_id) == 1

    # A second ETL import of the same file also dedupes.
    with Session(engine) as session:
        result2 = run_import(
            session,
            rows,
            _mapping(),
            ip_id=ip_id,
            batch_name="nullfp-2.xlsx",
            dry_run=False,
            batch_comment=None,
            chunk_size=None,
        )
        session.commit()
    assert result2["inserted"] == 0
    assert result2["duplicates"] == 1
    assert _op_count(engine, ip_id) == 1


def test_null_fingerprint_conflicting_content_is_loud_error(engine, applied_schema):
    """Same external_id but DIFFERENT content vs the manual NULL-fingerprint
    row: loud row error — never a silent double-book, never a silent skip."""
    ip_id = _seed_manual_null_fp(engine, "416", "NULLFP-CONFLICT")

    rows = [{"type": "income", "amount": "999", "date": "2026-03-10", "order_id": "MANUAL-1"}]
    with Session(engine) as session:
        result = run_import(
            session,
            rows,
            _mapping(),
            ip_id=ip_id,
            batch_name="nullfp-conflict.xlsx",
            dry_run=False,
            batch_comment=None,
            chunk_size=None,
        )
        session.commit()

    assert result["inserted"] == 0
    assert result["duplicates"] == 0
    assert result["errors"] == 1
    assert _op_count(engine, ip_id) == 1  # manual row untouched, nothing added


def test_etl_never_inserts_without_fingerprint(engine, applied_schema, monkeypatch):
    """The ETL invariant: if fingerprint generation ever yields an empty
    value, the row fails as an error — it is never inserted without one."""
    import etl.import_excel as ie

    bin_code = f"415{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('NULLFP-GUARD', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

    monkeypatch.setattr(ie, "row_fingerprint", lambda data: "")
    rows = [{"type": "income", "amount": "100", "date": "2026-03-11", "order_id": "G-1"}]
    with Session(engine) as session:
        result = run_import(
            session,
            rows,
            _mapping(),
            ip_id=ip_id,
            batch_name="guard.xlsx",
            dry_run=False,
            batch_comment=None,
            chunk_size=None,
        )
        session.commit()

    assert result["inserted"] == 0
    assert result["errors"] == 1
    # The failure reason is recorded on the batch (not silently dropped).
    with engine.begin() as conn:
        details = conn.execute(
            text("SELECT error_details FROM import_batches WHERE id = :b"),
            {"b": result["batch_id"]},
        ).scalar_one()
    assert "fingerprint" in str(details).lower()
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM operations WHERE ip_id = :ip"),
            {"ip": ip_id},
        ).scalar_one()
    assert count == 0
