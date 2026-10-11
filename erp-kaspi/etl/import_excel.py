"""
Excel ETL — Kaspi order exports into the ledger (MASTER PROMPT ШАГ 2, §28, §41).

Design constraints (traceability):
  * OQ-002 — the customer's Excel layout is unknown; every column name is read
    from a YAML mapping (see etl/column_map.example.yaml). Nothing is hard-coded.
  * §41 / ADR-010 — idempotency: every row carries a SHA-256 import_fingerprint
    over its normalised content; the partial unique index
    uq_operations_import_fingerprint makes a re-run skip what it already stored.
  * §38 — --dry-run parses, validates and counts, then rolls back: the database
    is left untouched (import_batches.status = 'DRY_RUN').
  * §94 — nothing is invented: a row whose type has no mapping is an ERROR row,
    not a guess.

Usage:
    python -m etl.import_excel --file sales.xlsx --ip 1 \
        [--map etl/column_map.example.yaml] [--dry-run] [--batch-comment "..."]

The CLI uses DATABASE_URL (or DATABASE_* variables, §10/§11).
"""

from __future__ import annotations

import argparse
import contextlib
import decimal
import hashlib
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_session_factory
from app.models.finance import ImportBatch, Operation
from app.models.reference import Category, OperationType

# --------------------------------------------------------------------------- #
# Parsing helpers — KZT money (§15) and ru/en dates
# --------------------------------------------------------------------------- #


def parse_amount(raw: str) -> Decimal:
    """'12 500,00' / '12500.00' / '12 500' / '(12500)' -> Decimal KZT (§15: KZT only).

    E-4: Excel accounting format renders negatives as (12500). Parsed as
    negative here, but the §27 sign is still forced by
    operation_types.direction downstream (run_import applies abs()), so
    the source file never dictates the booked sign.
    """
    text = str(raw).strip().replace("\u00a0", "").replace(" ", "")
    negative = False
    if text.startswith("(") and text.endswith(")"):
        # E-4: balanced accounting-format parens, e.g. "(12 500,00)".
        # Unbalanced parens fall through and fail at Decimal() below.
        text = text[1:-1]
        negative = True
    if "," in text and "." in text:
        # 1 234 567,89 vs 1,234,567.89 — the LAST separator is the decimal one
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    if not text:
        raise ValueError("empty amount")
    try:
        value = Decimal(text)
    except decimal.InvalidOperation as exc:
        raise ValueError(f"unparsable amount: {raw!r}") from exc
    if negative:
        value = -value
    if value.is_nan():
        # Pandas delivers an empty Excel cell as float('nan'); str() of it is
        # 'nan', which Decimal accepts. NaN is never a valid amount — treat
        # as empty so the row becomes a recorded batch error (§28).
        raise ValueError("empty amount")
    if value == 0:
        raise ValueError("§27: amount must not be zero")
    return value


def parse_date(raw) -> date:
    """dd.mm.yyyy or yyyy-mm-dd. Also accepts datetime/pandas Timestamp
    (Excel date-formatted cells arrive as datetime64, not text — E-1)."""
    # Excel date cells arrive as datetime/Timestamp, not strings.
    if isinstance(raw, datetime):
        return raw.date()
    try:
        import pandas as pd

        if isinstance(raw, pd.Timestamp):
            return raw.date()
    except ImportError:
        pass
    text = str(raw).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {raw!r}")


def row_fingerprint(data: dict[str, str]) -> str:
    """§41 — SHA-256 over the normalised row; identical rows import once.

    Amounts are normalised to 2 decimal places (E-2): '12500', '12500.0'
    and '12500.00' are the same logical amount and must fingerprint
    identically, otherwise re-imports with different formatting escape
    duplicate detection.
    """
    normalised = dict(data)
    amount = normalised.get("amount")
    if amount is not None:
        with contextlib.suppress(decimal.InvalidOperation):
            # keep raw on failure; the row will fail validation downstream
            normalised["amount"] = f"{Decimal(str(amount)):.2f}"
    canonical = json.dumps(normalised, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Workbook reading via the configurable mapping (OQ-002)
# --------------------------------------------------------------------------- #


def load_mapping(path: Path) -> dict:
    mapping = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in ("columns", "types"):
        if key not in mapping:
            raise ValueError(f"mapping file {path} is missing section '{key}'")
    return mapping


def read_rows(workbook_path: Path, mapping: dict) -> list[dict[str, str]]:
    """Read the sheet named by the mapping; headers come from header_row."""
    import pandas as pd

    sheet = mapping.get("sheet", 0)
    header_row = int(mapping.get("header_row", 0))
    frame = pd.read_excel(workbook_path, sheet_name=sheet, header=header_row)
    frame.columns = [str(c).strip() for c in frame.columns]
    # E-3: 'Сумма' and 'Сумма ' (trailing space) both strip to 'Сумма';
    # pandas silently drops the duplicate column → silent wrong data.
    # Fail fast instead of importing the wrong column's values.
    if len(set(frame.columns)) != len(frame.columns):
        dupes = sorted({c for c in frame.columns if list(frame.columns).count(c) > 1})
        raise ValueError(f"duplicate column headers after strip: {dupes}")

    columns = mapping["columns"]
    missing = [v for v in columns.values() if v not in frame.columns]
    if missing:
        raise ValueError(
            f"columns {missing} not found in the workbook; "
            f"found: {list(frame.columns)} — fix the mapping (OQ-002)"
        )

    rows: list[dict[str, str]] = []
    for record in frame.to_dict(orient="records"):
        row = {}
        for key, col in columns.items():
            raw_value = record.get(col)
            # Pandas represents an empty Excel cell as float('nan'), which is
            # neither None nor ""; pd.isna() catches NaN/NaT/None uniformly.
            row[key] = "" if raw_value is None or pd.isna(raw_value) else str(raw_value).strip()
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Import — one transaction per batch; dry-run rolls back (§38)
# --------------------------------------------------------------------------- #


def run_import(
    session: Session,
    rows: list[dict[str, str]],
    mapping: dict,
    *,
    ip_id: int,
    batch_name: str,
    dry_run: bool,
    batch_comment: str | None,
    chunk_size: int | None = None,
) -> dict[str, object]:
    """Run Excel import.

    Args:
        chunk_size: If set, commit every N rows (P0-2). Default None preserves
            the legacy single-transaction behavior. When chunking, the caller
            must NOT wrap in session.begin() — run_import manages transactions.
            Must be a positive int when given (F-ETL-004).
    """
    # F-ETL-004: fail fast on invalid chunk_size BEFORE any DB work, so the
    # caller gets a 422/CLI error instead of a mid-import 500 (chunk_size=0
    # crashed range()) or a silent 0-row SUCCESS (negative chunk_size made
    # range() empty). bool is rejected explicitly: isinstance(True, int).
    if chunk_size is not None and (
        not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1
    ):
        raise ValueError(f"chunk_size must be a positive integer or None, got {chunk_size!r}")

    types_map: dict[str, str] = mapping["types"]
    categories_map: dict[str, str | None] = mapping.get("categories", {})
    comment_prefix = mapping.get("defaults", {}).get("comment_prefix", "Excel import")

    batch = ImportBatch(
        source_type="EXCEL",
        source_name=batch_name,
        ip_id=ip_id,
        is_dry_run=dry_run,
        status="RUNNING",
    )
    session.add(batch)
    session.flush()

    # type code -> id; unknown codes are an ERROR, never a guess (§94)
    type_ids = {}
    type_directions = {}
    for code in set(types_map.values()):
        row = session.execute(
            select(OperationType.id, OperationType.direction).where(OperationType.code == code)
        ).first()
        if row is not None:
            type_ids[code], type_directions[code] = row
    category_ids = {
        code: session.scalar(select(Category.id).where(Category.code == code))
        for code in {c for c in categories_map.values() if c}
    }

    counters = {"total": 0, "inserted": 0, "skipped": 0, "duplicates": 0, "errors": 0}
    errors: list[dict[str, str]] = []

    # P0-1: batch-load existing fingerprints in ONE query instead of N+1.
    # Pre-compute fingerprints for all rows (cheap, no DB), then fetch.
    def _fp_for(row: dict) -> str | None:
        try:
            tcode = types_map.get(row["type"])
            if tcode is None:
                return None
            amt = parse_amount(row["amount"])
            amt = -abs(amt) if type_directions.get(tcode) == "OUT" else abs(amt)
            odt = parse_date(row["date"])
            ext = row.get("order_id") or None
            return row_fingerprint(
                {
                    "ip_id": ip_id,
                    "date": odt.isoformat(),
                    "type": tcode,
                    "amount": str(amt),
                    "external_id": ext or "",
                }
            )
        except Exception:
            return None

    all_fps = {_fp_for(r) for r in rows}
    all_fps.discard(None)
    existing_fps: set[str] = set()
    if all_fps:
        # Scope to this IP (per-IP unique index since 0011)
        # P0-2: Batch the IN clause to avoid oversized queries at 100k+ scale.
        # PostgreSQL has a parameter limit; chunk at 1000 fingerprints per query.
        fp_list = list(all_fps)
        for i in range(0, len(fp_list), 1000):
            batch_fps = fp_list[i:i + 1000]
            existing_fps.update(
                session.scalars(
                    select(Operation.import_fingerprint).where(
                        Operation.ip_id == ip_id,
                        Operation.import_fingerprint.in_(batch_fps),
                    )
                )
            )
    # Track fingerprints added in this batch (avoid double-insert within the file)
    seen_fps: set[str] = set()

    def _process_row(index: int, row: dict) -> None:
        """Process a single row. Updates counters, errors, seen_fps in closure."""
        counters["total"] += 1
        try:
            type_code = types_map.get(row["type"])
            if type_code is None:
                raise ValueError(f"no mapping for type {row['type']!r} (§94)")
            type_id = type_ids.get(type_code)
            if type_id is None:
                raise ValueError(f"operation type {type_code!r} not in the database")

            amount = parse_amount(row["amount"])
            # §27 sign convention: IN -> positive, OUT -> negative. The source
            # file never dictates the sign; operation_types.direction does.
            amount = -abs(amount) if type_directions.get(type_code) == "OUT" else abs(amount)
            operation_date = parse_date(row["date"])
            external_id = row.get("order_id") or None

            category_id = None
            if type_code in categories_map:
                cat_code = categories_map[type_code]
                category_id = category_ids.get(cat_code) if cat_code else None

            fingerprint = row_fingerprint(
                {
                    "ip_id": ip_id,
                    "date": operation_date.isoformat(),
                    "type": type_code,
                    "amount": str(amount),
                    "external_id": external_id or "",
                }
            )
            # NULL-fingerprint guard: every ETL-imported row MUST carry an
            # idempotency fingerprint. row_fingerprint() always returns a
            # 64-char hex — if it ever yields an empty value, fail the row
            # loudly instead of inserting a row the dedup index cannot see
            # (partial index ignores NULLs; "" would collide on re-import).
            if not fingerprint:
                raise ValueError(
                    f"refusing to import row {index} without an idempotency fingerprint"
                )

            # §41 — idempotency: same fingerprint -> skip, never double-book.
            # P0-1: checked against the pre-loaded set (one query total),
            # plus in-batch seen_fps for duplicates within the file.
            if fingerprint in existing_fps or fingerprint in seen_fps:
                counters["duplicates"] += 1
                return
            seen_fps.add(fingerprint)

            op = Operation(
                ip_id=ip_id,
                type_id=type_id,
                category_id=category_id,
                amount=amount,
                operation_date=operation_date,
                # ETL: cash basis by default (accrual_date = operation_date).
                # The Excel date semantic is undefined; backfill policy applies.
                accrual_date=operation_date,
                external_id=external_id,
                import_batch_id=batch.id,
                import_fingerprint=fingerprint,
                comment=f"{comment_prefix}: {batch_name} row {index}",
            )
            # F-ETL-008: Flush per-row to catch concurrent duplicate fingerprints.
            # If another process inserted the same fingerprint between our
            # pre-check and this insert, the unique index raises IntegrityError.
            # Treat as duplicate (not error) — idempotency preserved.
            # CRITICAL: add()+flush() must happen INSIDE the savepoint.
            # begin_nested() autoflushes pending objects in _take_snapshot
            # BEFORE the SAVEPOINT exists — a flush there kills the whole
            # transaction instead of rolling back to the savepoint.
            try:
                with session.begin_nested():
                    session.add(op)
                    session.flush()
            except IntegrityError as exc:
                # The savepoint rollback detaches the failed row; if it
                # lingers in session.new, expunge it — a lingering pending
                # row would poison every later flush with a retried INSERT.
                if op in session.new:
                    session.expunge(op)
                orig = str(exc.orig or exc)
                if "uq_operations_import_fingerprint" in orig:
                    counters["duplicates"] += 1
                    return
                if "uq_operations_ip_type_external" in orig:
                    # Concurrent import of the same source document: PG may
                    # check this constraint before the fingerprint index, so
                    # the loser must not escape as an error. Duplicate iff the
                    # existing row's content matches (same fingerprint). For
                    # legacy/manual rows with NULL fingerprint, recompute it
                    # from their content with the canonical scheme first — a
                    # content mismatch stays a visible error, never a silent
                    # double-book and never a silent skip.
                    existing = session.scalar(
                        select(Operation).where(
                            Operation.ip_id == ip_id,
                            Operation.type_id == type_id,
                            Operation.external_id == external_id,
                        )
                    )
                    if existing is not None:
                        existing_fp = existing.import_fingerprint or row_fingerprint(
                            {
                                "ip_id": existing.ip_id,
                                "date": existing.operation_date.isoformat(),
                                "type": type_code,
                                "amount": str(existing.amount),
                                "external_id": existing.external_id or "",
                            }
                        )
                        if existing_fp == fingerprint:
                            counters["duplicates"] += 1
                            return
                raise  # Not a duplicate — real error, counted by the handler below
            counters["inserted"] += 1
        except Exception as exc:
            counters["errors"] += 1
            errors.append({"row": str(index), "error": f"{type(exc).__name__}: {exc}"})

    def _update_batch_progress() -> None:
        """Update batch counters in DB (for chunked commits)."""
        batch.rows_total = counters["total"]
        batch.rows_inserted = counters["inserted"]
        batch.rows_skipped = counters["skipped"]
        batch.rows_duplicates = counters["duplicates"]
        batch.rows_errors = counters["errors"]
        # Don't set final status yet — only progress

    if chunk_size is None or dry_run:
        # Legacy single-transaction mode (or dry-run: never commit)
        for index, row in enumerate(rows, start=2):
            _process_row(index, row)
    else:
        # Chunked mode: commit every chunk_size rows.
        # Resume-safe: fingerprints prevent duplicates on retry.
        # Each chunk is atomic; failed chunks don't affect committed ones.
        for chunk_start in range(0, len(rows), chunk_size):
            chunk = rows[chunk_start:chunk_start + chunk_size]
            try:
                with session.begin_nested():  # SAVEPOINT per chunk
                    for i, row in enumerate(chunk, start=chunk_start + 2):
                        _process_row(i, row)
                    _update_batch_progress()
                session.commit()  # Commit the chunk
                # Refresh existing_fps with newly committed fingerprints
                # (seen_fps already covers in-memory, but DB needs refresh for resume)
            except Exception as exc:
                session.rollback()
                # Mark chunk as failed, continue with next chunk
                # (errors already recorded per-row; chunk failure is catastrophic)
                errors.append({
                    "row": f"chunk {chunk_start//chunk_size}",
                    "error": f"ChunkFailed: {type(exc).__name__}: {exc}"
                })
                counters["errors"] += len(chunk)
                # Re-raise if it's not a row-level error (DB connection lost, etc.)
                # For now, continue — batch status will be PARTIAL/FAILED

    batch.rows_total = counters["total"]
    batch.rows_inserted = counters["inserted"]
    batch.rows_skipped = counters["skipped"]
    batch.rows_duplicates = counters["duplicates"]
    batch.rows_errors = counters["errors"]
    batch.finished_at = datetime.now(UTC)
    # NB: tz-aware — finished_at is TIMESTAMPTZ and chk_import_batches_finished
    # compares against started_at (DB CURRENT_TIMESTAMP). A naive
    # datetime.now() breaks the CHECK when the OS and DB timezones differ.
    if errors:
        batch.error_details = {"errors": errors[:50]}  # cap the JSONB payload
    if counters["errors"] and counters["inserted"]:
        batch.status = "PARTIAL"
    elif counters["errors"]:
        batch.status = "FAILED"
    elif dry_run:
        batch.status = "DRY_RUN"
    else:
        batch.status = "SUCCESS"

    return {
        "batch_id": batch.id,
        "status": batch.status,
        **counters,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m etl.import_excel",
        description="Import a Kaspi/Excel workbook into the ledger (§28).",
    )
    parser.add_argument("--file", required=True, help="path to the .xlsx file")
    parser.add_argument("--ip", type=int, required=True, help="ИП id (ip_entities.id)")
    parser.add_argument(
        "--map",
        dest="mapping_path",
        default=str(Path(__file__).with_name("column_map.example.yaml")),
        help="YAML column mapping (see etl/column_map.example.yaml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate and count, write nothing (§38)"
    )
    parser.add_argument("--batch-comment", default=None)
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help="P0-2: commit every N rows (resume-capable). Default None = single transaction.",
    )
    args = parser.parse_args()

    workbook_path = Path(args.file)
    if not workbook_path.is_file():
        print(f"ERROR: file not found: {workbook_path}", file=sys.stderr)
        return 2
    mapping = load_mapping(Path(args.mapping_path))
    rows = read_rows(workbook_path, mapping)

    factory = get_session_factory()
    if args.chunk_size is not None and not args.dry_run:
        # Chunked mode: run_import manages its own transactions per chunk.
        # Do NOT wrap in session.begin() — each chunk commits independently.
        # Resume-safe: fingerprints prevent duplicates on retry.
        with factory() as session:
            result = run_import(
                session,
                rows,
                mapping,
                ip_id=args.ip,
                batch_name=workbook_path.name,
                dry_run=args.dry_run,
                batch_comment=args.batch_comment,
                chunk_size=args.chunk_size,
            )
    else:
        # Legacy single-transaction mode (or dry-run)
        with factory() as session, session.begin():
            result = run_import(
                session,
                rows,
                mapping,
                ip_id=args.ip,
                batch_name=workbook_path.name,
                dry_run=args.dry_run,
                batch_comment=args.batch_comment,
            )
            if args.dry_run:
                # §38 — dry-run leaves no rows: the whole transaction is undone,
                # only the counters are reported.
                session.rollback()
                result["status"] = "DRY_RUN"

    print(result)
    return 0 if result["status"] in ("SUCCESS", "DRY_RUN") else 1


if __name__ == "__main__":
    sys.exit(main())
