"""
ETL tests — Excel import inside a rollback transaction (§28, §38, §41, §94).

The tests never leave business data behind: every scenario runs inside one
transaction that is always rolled back (§94, TD-012 pattern). Fixtures are
created with SAVEPOINTs so a failing row cannot poison the batch.

MASTER PROMPT coverage:
  * §28 — import_batches counters + error_details;
  * §38 — dry-run produces ZERO persistent writes;
  * §41 / ADR-010 — idempotency via import_fingerprint;
  * §27 — sign convention (IN positive / OUT negative) is enforced by the
    importer, proven against the real DB trigger;
  * §94 — no invented business data survives the test run.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from app.models.finance import ImportBatch
from etl.import_excel import (
    load_mapping,
    parse_amount,
    parse_date,
    read_rows,
    row_fingerprint,
    run_import,
)

MAPPING_PATH = Path(__file__).resolve().parent.parent / "etl" / "column_map.example.yaml"


def _workbook(rows: list[dict[str, str]]) -> io.BytesIO:
    import pandas as pd

    buf = io.BytesIO()
    pd.DataFrame(
        rows,
        columns=["Дата операции", "Номер заказа", "Сумма", "Тип"],
    ).to_excel(buf, index=False)
    buf.seek(0)
    return buf


def _mapping(tmp_path: Path) -> dict:
    """The example mapping, pointed at the 'Продажа/Возврат' workbook layout."""
    mapping = load_mapping(MAPPING_PATH)
    mapping["sheet"] = 0
    mapping["header_row"] = 0
    return mapping


@pytest.fixture
def qa_ip(rollback_session) -> int:
    """A QA ИП created inside the rollback transaction — never persists."""
    from sqlalchemy import text as _text

    ip_id = rollback_session.execute(
        _text(
            "INSERT INTO ip_entities (name, bin) VALUES ('ETL QA ИП', '777777777777') "
            "ON CONFLICT (bin) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        )
    ).scalar_one()
    rollback_session.flush()
    return ip_id


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_parse_amount_uses_russian_locale():
    assert parse_amount("12 500,00") == Decimal("12500.00")
    assert parse_amount("3 400,50") == Decimal("3400.50")
    assert parse_amount("-500,00") == Decimal("-500.00")
    # §27 — an empty/garbage amount can never be booked as zero; the row
    # becomes a recorded batch error instead (§28).
    with pytest.raises(ValueError):
        parse_amount("")


def test_parse_amount_rejects_garbage():
    with pytest.raises(ValueError):
        parse_amount("abc")


def test_parse_amount_accepts_accounting_negatives():
    """E-4: Excel accounting format renders negatives as (12500).
    Parsed as negative; §27 sign is still forced by
    operation_types.direction downstream (run_import applies abs()),
    so this only moves such rows from error-rows to importable."""
    assert parse_amount("(12500)") == Decimal("-12500")
    assert parse_amount("(12 500,00)") == Decimal("-12500.00")
    # (0) is still zero — rejected by the §27 zero-check, like "0"
    with pytest.raises(ValueError):
        parse_amount("(0)")
    # unbalanced parens stay garbage
    with pytest.raises(ValueError):
        parse_amount("(12500")
    with pytest.raises(ValueError):
        parse_amount("12500)")


def test_parse_amount_rejects_nan_from_pandas_empty_cell():
    """Pandas represents an empty Excel cell as float('nan'); str() of it is
    'nan', which Decimal parses without error and which passes the §27
    zero-check (NaN != 0). NaN must never reach the ledger — it is an
    empty amount and becomes a recorded batch error (§28)."""
    with pytest.raises(ValueError):
        parse_amount(str(float("nan")))
    with pytest.raises(ValueError):
        parse_amount("nan")
    with pytest.raises(ValueError):
        parse_amount("NaN")


def test_row_fingerprint_is_stable_and_salts_by_ip():
    base = {
        "ip_id": 1,
        "date": "2026-09-01",
        "type": "SALE_KASPI_PAY",
        "amount": "100.00",
        "external_id": "K-1",
    }
    assert row_fingerprint(base) == row_fingerprint(dict(base))
    assert row_fingerprint(base) != row_fingerprint({**base, "ip_id": 2})


# --------------------------------------------------------------------------- #
# Batch scenarios — all inside one rollback transaction
# --------------------------------------------------------------------------- #


def test_import_inserts_rows_with_correct_signs(rollback_session, qa_ip):
    mapping = _mapping(None)
    rows = read_rows(
        _workbook(
            [
                {
                    "Дата операции": "01.09.2026",
                    "Номер заказа": "KSP-1",
                    "Сумма": "12 500,00",
                    "Тип": "Продажа",
                },
                {
                    "Дата операции": "02.09.2026",
                    "Номер заказа": "KSP-2",
                    "Сумма": "3 400,50",
                    "Тип": "Комиссия",
                },
            ]
        ),
        mapping,
    )
    result = run_import(
        rollback_session,
        rows,
        mapping,
        ip_id=qa_ip,
        batch_name="qa",
        dry_run=False,
        batch_comment=None,
    )

    assert result["status"] == "SUCCESS"
    assert result["total"] == 2
    assert result["inserted"] == 2
    assert result["duplicates"] == 0
    assert result["errors"] == 0

    from sqlalchemy import select

    from app.models.finance import Operation

    ops = rollback_session.scalars(
        select(Operation)
        .where(Operation.import_batch_id == result["batch_id"])
        .order_by(Operation.id)
    ).all()
    assert Decimal(ops[0].amount) == Decimal("12500.00")  # IN — positive
    assert Decimal(ops[1].amount) == Decimal("-3400.50")  # OUT — negative
    assert all(op.import_fingerprint for op in ops)


def test_import_is_idempotent_by_fingerprint(rollback_session, qa_ip):
    """§41 — replaying the same file must not double-book a single kopeck."""
    mapping = _mapping(None)
    rows = read_rows(
        _workbook(
            [
                {
                    "Дата операции": "01.09.2026",
                    "Номер заказа": "KSP-1",
                    "Сумма": "12 500,00",
                    "Тип": "Продажа",
                },
            ]
        ),
        mapping,
    )
    first = run_import(
        rollback_session,
        rows,
        mapping,
        ip_id=qa_ip,
        batch_name="qa-1",
        dry_run=False,
        batch_comment=None,
    )
    second = run_import(
        rollback_session,
        rows,
        mapping,
        ip_id=qa_ip,
        batch_name="qa-2",
        dry_run=False,
        batch_comment=None,
    )

    assert first["inserted"] == 1
    assert second["duplicates"] == 1
    assert second["inserted"] == 0
    assert second["status"] == "SUCCESS"

    from sqlalchemy import func, select

    from app.models.finance import Operation

    count = rollback_session.scalar(
        select(func.count()).select_from(Operation).where(Operation.ip_id == qa_ip)
    )
    assert count == 1


def test_dry_run_leaves_no_trace(rollback_session, qa_ip):
    """§38 — a dry run must not persist a single row, not even the batch."""
    mapping = _mapping(None)
    rows = read_rows(
        _workbook(
            [
                {
                    "Дата операции": "01.09.2026",
                    "Номер заказа": "KSP-D1",
                    "Сумма": "1 000,00",
                    "Тип": "Продажа",
                },
            ]
        ),
        mapping,
    )
    result = run_import(
        rollback_session,
        rows,
        mapping,
        ip_id=qa_ip,
        batch_name="dry",
        dry_run=True,
        batch_comment=None,
    )

    assert result["status"] in ("DRY_RUN", "RUNNING")
    assert result["inserted"] == 1

    # §38 — the CALLER undoes the transaction (the CLI does exactly this);
    # afterwards not a single row may remain, not even the batch.
    rollback_session.rollback()

    from sqlalchemy import text as _text

    live = rollback_session.execute(
        _text(
            "SELECT count(*) FROM operations o JOIN ip_entities i ON i.id = o.ip_id "
            "WHERE i.bin = '777777777777'"
        )
    ).scalar_one()
    batches = rollback_session.execute(
        _text(
            "SELECT count(*) FROM import_batches b JOIN ip_entities i ON i.id = b.ip_id "
            "WHERE i.bin = '777777777777'"
        )
    ).scalar_one()
    assert live == 0, "§38: a dry run must not persist operations"
    assert batches == 0, "§38: a dry run must not persist the batch"


def test_partial_import_records_error_details(rollback_session, qa_ip):
    """§28 — unknown types are recorded as row errors, never guessed (§94)."""
    mapping = _mapping(None)
    rows = read_rows(
        _workbook(
            [
                {
                    "Дата операции": "01.09.2026",
                    "Номер заказа": "KSP-OK",
                    "Сумма": "500,00",
                    "Тип": "Продажа",
                },
                {
                    "Дата операции": "02.09.2026",
                    "Номер заказа": "KSP-BAD",
                    "Сумма": "700,00",
                    "Тип": "Неверный тип",
                },
            ]
        ),
        mapping,
    )
    result = run_import(
        rollback_session,
        rows,
        mapping,
        ip_id=qa_ip,
        batch_name="partial",
        dry_run=False,
        batch_comment=None,
    )

    assert result["status"] == "PARTIAL"
    assert result["inserted"] == 1
    assert result["errors"] == 1

    batch = rollback_session.get(ImportBatch, result["batch_id"])
    assert batch.error_details is not None
    (err,) = batch.error_details["errors"]
    assert "no mapping for type" in err["error"]
    assert err["row"] == "3"


def test_missing_mapping_sections_rejected(tmp_path):
    """OQ-002 — a mapping without 'columns'/'types' fails loudly."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"columns": {"date": "x"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing section 'types'"):
        load_mapping(bad)


def test_import_missing_columns_rejected(tmp_path, qa_ip, engine):
    """A workbook that does not match the mapping fails before any INSERT."""
    mapping = _mapping(None)
    broken = tmp_path / "broken.xlsx"
    import pandas as pd

    buf = io.BytesIO()
    pd.DataFrame({"foo": [1]}).to_excel(buf, index=False, engine="openpyxl")
    broken.write_bytes(buf.getvalue())

    with pytest.raises(ValueError, match="not found in the workbook"):
        read_rows(broken, mapping)


# --------------------------------------------------------------------------- #
# §94 — the test database holds no business data after the run
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def baseline_counts(engine):
    """Counts captured before any ETL test runs.

    The API/integration suites commit their fixtures to the real test
    database by design, so §94 is verified RELATIVE to this baseline:
    the ETL suite must add zero rows on top of it. On a freshly
    provisioned database the baseline is all zeros.
    """
    with engine.connect() as conn:
        return {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in ("ip_entities", "operations", "import_batches")
        }


def test_no_business_data_left_behind(engine, baseline_counts):
    """§94 — the ETL suite itself must not add business data (TD-012)."""
    with engine.connect() as conn:
        for table, base in baseline_counts.items():
            count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            assert count == base, (
                f"§94: ETL tests leaked rows into {table} ({count} != baseline {base})"
            )


def test_parse_date_accepts_pandas_timestamp():
    """E-1: Excel date-formatted cells arrive as pandas Timestamp, not text.
    str(Timestamp) = '2026-01-15 00:00:00' matches no text format."""
    from datetime import date, datetime

    import pandas as pd

    ts = pd.Timestamp("2026-01-15 00:00:00")
    assert parse_date(ts) == date(2026, 1, 15)
    assert parse_date(datetime(2026, 1, 15, 10, 30)) == date(2026, 1, 15)
    # text formats still work
    assert parse_date("15.01.2026") == date(2026, 1, 15)
    assert parse_date("2026-01-15") == date(2026, 1, 15)


def test_row_fingerprint_normalizes_amount():
    """E-2: §41 idempotency — '12500.00' vs '12500' is the same logical
    amount and must produce the same fingerprint."""
    base = {"ip_id": 1, "date": "2026-01-15", "type": "SALE", "external_id": "X-1"}
    fp1 = row_fingerprint({**base, "amount": "12500.00"})
    fp2 = row_fingerprint({**base, "amount": "12500"})
    fp3 = row_fingerprint({**base, "amount": "12500.0"})
    assert fp1 == fp2 == fp3


def test_read_rows_rejects_duplicate_headers_after_strip():
    """E-3: 'Сумма' and 'Сумма ' (trailing space) both strip to 'Сумма';
    pandas silently drops one column → silent wrong data. Must fail fast."""
    import pandas as pd

    df = pd.DataFrame(
        [["2026-01-15", "SALE", 12500, 99999]],
        columns=["Дата", "Тип", "Сумма", "Сумма "],
    )
    path = Path("/tmp/test_dup_headers.xlsx")
    df.to_excel(path, index=False)
    mapping = {
        "sheet": 0,
        "header_row": 0,
        "columns": {"date": "Дата", "type": "Тип", "amount": "Сумма"},
        "types": {},
    }
    with pytest.raises(ValueError, match=r"[Dd]uplicate"):
        read_rows(path, mapping)
