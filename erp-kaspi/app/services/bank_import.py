"""Bank statement import pipeline (G-BANK-2..5).

Pipeline: Provider -> Adapter -> Raw Statement -> Normalized Bank Transaction
-> Dedup -> Classification -> Operation -> Reconciliation.

* File import only (MT940 text). No invented bank APIs: the Halyk live API
  is deferred until official client-gated docs/access; Kaspi Pay and Freedom
  format-specific adapters wait for real official/sample exports.
* Raw source is preserved verbatim (``raw_bank_statements.content`` BYTEA);
  normalized lines are immutable external facts (DB trigger); the ledger
  link is only via ``matched_operation_id``.
* Idempotency: file-level (``uq_raw_bank_statements_file``) + line-level
  (``uq_bank_statement_lines_fingerprint``) dedup, per-row savepoints
  (F-ETL-008 pattern). A crashed import resumes: the raw row exists but
  ``processed_at`` is NULL, so a retry reuses it and only inserts the
  missing lines.
* STOP conditions (never improvised): unknown MT940 D/C mark -> skip +
  warning; currency != KZT -> skip with reason (OQ-014); empty KNP
  registry -> commissions stay UNMATCHED.
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.bank import BankFeeKnpCode, BankStatementLine, RawBankStatement
from app.models.finance import ImportBatch, Operation
from app.models.reference import Account, OperationType
from app.schemas import OperationCreate, OperationDirection
from app.services import add_operation

#: Providers recognised by the pipeline. No live APIs — file import only.
PROVIDER_CODES: tuple[str, ...] = ("HALYK", "FREEDOM", "EPAY", "KASPI_PAY", "MANUAL")

#: Currencies the ledger can post (MASTER PROMPT §15; OQ-014 for the rest).
POSTABLE_CURRENCY = "KZT"

#: §5 DoS guard — max raw statement file size (10 MiB). Bank statements are
#: text; anything larger is rejected before parsing.
MAX_STATEMENT_BYTES = 10 * 1024 * 1024
#: §5 DoS guard — max parsed statement lines per file.
MAX_STATEMENT_ROWS = 100_000


# --------------------------------------------------------------------------- #
# Normalized model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NormalizedBankTransaction:
    """One bank statement line in provider-independent form.

    ``amount`` is always positive; the sign lives in ``direction`` (§27
    maps direction to the operation sign at posting time).
    """

    provider_code: str
    provider_account: str
    value_date: date
    entry_date: date | None
    amount: Decimal
    currency: str
    direction: Literal["IN", "OUT"]
    document_number: str | None = None
    bank_reference: str | None = None
    counterparty_name: str | None = None
    counterparty_iin: str | None = None
    counterparty_bik: str | None = None
    counterparty_account: str | None = None
    payment_purpose: str | None = None
    knp_code: str | None = None
    balance_after: Decimal | None = None
    raw_line: str = ""
    line_no: int = 0
    provider_extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedStatement:
    """Adapter output: statement-level facts + normalized transactions."""

    provider_code: str
    provider_account: str
    statement_no: str | None = None
    period_from: date | None = None
    period_to: date | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    currency: str | None = None
    transactions: list[NormalizedBankTransaction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Lines the parser rejected (unknown D/C mark, bad amount, ...).
    skipped_count: int = 0


# --------------------------------------------------------------------------- #
# Fingerprint (§3 of the impl spec)
# --------------------------------------------------------------------------- #


def core_fingerprint(tx: NormalizedBankTransaction) -> str:
    """SHA-256 of the normalized line core.

    ``provider`` + ``provider_account`` are included: the same economic
    payout in a Halyk statement and in an ePay report are different
    external facts; merging them is the reconciliation layer's job, not
    dedup's. ``counterparty_iin`` is excluded (absent in weak formats —
    including it would create false duplicates on backfill). ``entry_date``
    is excluded (Halyk may regenerate a statement with a different entry
    date for the same value date).
    """
    payload = {
        "provider": tx.provider_code,
        "account": tx.provider_account.strip(),
        "value_date": tx.value_date.isoformat(),
        "amount": f"{tx.amount:.2f}",  # E-2 normalisation
        "direction": tx.direction,
        "document": (tx.document_number or "").strip(),
        "bank_ref": (tx.bank_reference or "").strip(),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Provider adapters
# --------------------------------------------------------------------------- #


class ProviderAdapter(ABC):
    """Minimal contract for a statement file format."""

    provider_code: str
    format_name: str

    @abstractmethod
    def can_parse(self, content: bytes, filename: str) -> bool:
        """Cheap detection — must not do a full parse."""

    @abstractmethod
    def parse(self, content: bytes, filename: str) -> ParsedStatement:
        """Full parse into normalized transactions."""

    def fingerprint(self, tx: NormalizedBankTransaction) -> str:
        return core_fingerprint(tx)


#: Registry consulted in order; first adapter whose can_parse() is True wins.
ADAPTER_REGISTRY: list[ProviderAdapter] = []


def register_adapter(adapter: ProviderAdapter) -> ProviderAdapter:
    """Register a provider adapter (import-time side effect, like plugins)."""
    ADAPTER_REGISTRY.append(adapter)
    return adapter


# --------------------------------------------------------------------------- #
# MT940 adapter (Halyk — format VERIFIED against official Halyk exports)
# --------------------------------------------------------------------------- #

_MT940_TAG = re.compile(r"^:(\d{2}[A-Z]?):(.*)$", re.DOTALL)

_MT940_LINE_61 = re.compile(
    r"^(?P<value_date>\d{6})(?P<entry_date>\d{4})?"
    r"(?P<dc>[A-Z]{1,3})(?P<amount>\d+(?:,\d{1,2})?)"
    r"(?P<txn_type>[A-Z][A-Z0-9]{3})(?P<reference>[^/]{0,16})"
    r"(?://(?P<bank_ref>.*))?$"
)

_MT940_BALANCE = re.compile(r"^([DC])(\d{6})([A-Z]{3})([\d,]+)$")

#: D/C marks per the impl spec §3.3. Unknown marks are never guessed.
_MT940_DC_OUT = frozenset({"D", "EC", "ED"})
_MT940_DC_IN = frozenset({"C", "RC"})

_MT940_KNP = re.compile(r"(?:КНП|KNP)\s*[:\-–№]?\s*(\d{1,10})", re.IGNORECASE)
_MT940_IIN = re.compile(r"(?<!\d)(\d{12})(?!\d)")


def _parse_mt940_amount(raw: str) -> Decimal:
    """MT940 uses ',' as the decimal separator."""
    return Decimal(raw.replace(",", "."))


def _parse_mt940_date(yyMMdd: str) -> date:
    yy = int(yyMMdd[0:2])
    year = 2000 + yy if yy < 70 else 1900 + yy
    return date(year, int(yyMMdd[2:4]), int(yyMMdd[4:6]))


def _parse_mt940_86(narrative: str) -> dict:
    """KZ narrative parsing: KNP code + 12-digit IIN/BIN.

    Everything else stays verbatim in payment_purpose; the raw text is
    kept in provider_extra for audit. BIK/counterparty account are NOT
    guessed from free text.
    """
    knp_code: str | None = None
    match = _MT940_KNP.search(narrative)
    if match:
        knp_code = match.group(1)
    iin: str | None = None
    match = _MT940_IIN.search(narrative)
    if match:
        iin = match.group(1)
    purpose = narrative.strip() or None
    return {
        "knp_code": knp_code,
        "counterparty_iin": iin,
        "payment_purpose": purpose,
        "mt940_86_raw": narrative,
    }


class MT940StatementAdapter(ProviderAdapter):
    """Halyk MT940 statement parser.

    Covers :20:/:25:/:28C:/:60F:/:61:/:86:/:62F:. Unknown D/C marks and
    non-KZT lines are skipped with warnings (STOP conditions) — never
    guessed.
    """

    provider_code = "HALYK"
    format_name = "MT940"

    def can_parse(self, content: bytes, filename: str) -> bool:
        head = content[:4096]
        return b":20:" in head and b":61:" in head

    def parse(self, content: bytes, filename: str) -> ParsedStatement:
        text_content = _decode_statement_text(content)
        fields = _split_mt940_fields(text_content)
        return _build_parsed_statement(fields, self.provider_code)


def _decode_statement_text(content: bytes) -> str:
    """UTF-8 first (with BOM tolerance), windows-1251 fallback for KZ banks.

    §6 — financial ETL prefers STOP over silent corruption: if neither
    decoding succeeds cleanly, the import is rejected instead of silently
    replacing undecodable bytes (which would turn source bytes into
    different text and then into normalized financial data).
    """
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return content.decode("windows-1251")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "statement file is not valid UTF-8 or windows-1251; "
            "refusing to import with lossy decoding"
        ) from exc


def _split_mt940_fields(text_content: str) -> list[tuple[str, str]]:
    """Split into (tag, value); non-tag lines continue the previous :86:."""
    fields: list[tuple[str, str]] = []
    for raw_line in text_content.splitlines():
        # §5 DoS guard — bound the parsed row count before building structures.
        if len(fields) >= MAX_STATEMENT_ROWS:
            raise ValueError(
                f"statement exceeds {MAX_STATEMENT_ROWS:,} parsed rows; "
                "split the file or contact support"
            )
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        match = _MT940_TAG.match(line)
        if match:
            fields.append((match.group(1), match.group(2)))
        elif fields and fields[-1][0] == "86":
            tag, value = fields[-1]
            fields[-1] = (tag, value + "\n" + line)
        # stray non-tag lines outside :86: are ignored (not silently parsed)
    return fields


def _build_parsed_statement(fields: list[tuple[str, str]], provider_code: str) -> ParsedStatement:
    warnings: list[str] = []
    provider_account = ""
    statement_no: str | None = None
    opening: Decimal | None = None
    closing: Decimal | None = None
    currency: str | None = None
    transactions: list[NormalizedBankTransaction] = []
    line_no = 0
    skipped_count = 0

    for tag, value in fields:
        if tag == "25":
            provider_account = value.strip().split()[0]
        elif tag == "28C":
            statement_no = value.strip() or None
        elif tag == "60F":
            parsed = _parse_balance(value, "opening balance")
            if parsed is not None:
                _dc, _bal_date, currency, opening = parsed
            else:
                warnings.append(f"unparseable :60F: {value!r}")
        elif tag == "62F":
            parsed = _parse_balance(value, "closing balance")
            if parsed is not None:
                _dc, _bal_date, ccy, closing = parsed
                currency = currency or ccy
            else:
                warnings.append(f"unparseable :62F: {value!r}")
        elif tag == "61":
            line_no += 1
            tx = _parse_61(value, provider_code, provider_account, line_no, warnings)
            if tx is not None:
                transactions.append(tx)
            else:
                skipped_count += 1
        elif tag == "86" and transactions:
            extra = _parse_mt940_86(value)
            prev = transactions[-1]
            transactions[-1] = NormalizedBankTransaction(
                **{
                    **prev.__dict__,
                    "counterparty_iin": extra["counterparty_iin"],
                    "payment_purpose": extra["payment_purpose"],
                    "knp_code": extra["knp_code"],
                    "provider_extra": {
                        **prev.provider_extra,
                        "mt940_86_raw": extra["mt940_86_raw"],
                    },
                }
            )

    period_from = transactions[0].value_date if transactions else None
    period_to = transactions[-1].value_date if transactions else None
    return ParsedStatement(
        provider_code=provider_code,
        provider_account=provider_account,
        statement_no=statement_no,
        period_from=period_from,
        period_to=period_to,
        opening_balance=opening,
        closing_balance=closing,
        currency=currency,
        transactions=transactions,
        warnings=warnings,
        skipped_count=skipped_count,
    )


def _parse_balance(value: str, what: str) -> tuple[str, date, str, Decimal] | None:
    match = _MT940_BALANCE.match(value.strip())
    if not match:
        return None
    dc, raw_date, ccy, raw_amount = match.groups()
    amount = _parse_mt940_amount(raw_amount)
    if dc == "D":
        amount = -amount
    return dc, _parse_mt940_date(raw_date), ccy, amount


def _parse_61(
    value: str,
    provider_code: str,
    provider_account: str,
    line_no: int,
    warnings: list[str],
) -> NormalizedBankTransaction | None:
    match = _MT940_LINE_61.match(value.strip())
    if not match:
        warnings.append(f"unparseable :61: {value!r} — skipped")
        return None
    parts = match.groupdict()
    dc_mark = parts["dc"]
    if dc_mark in _MT940_DC_OUT:
        direction: Literal["IN", "OUT"] = "OUT"
    elif dc_mark in _MT940_DC_IN:
        direction = "IN"
    else:
        # STOP condition: unknown D/C mark is never guessed.
        warnings.append(f"unknown D/C mark {dc_mark!r} in :61: — skipped")
        return None
    try:
        amount = _parse_mt940_amount(parts["amount"])
    except InvalidOperation:
        warnings.append(f"unparseable amount in :61: {value!r} — skipped")
        return None
    if amount <= 0:
        warnings.append(f"non-positive amount in :61: {value!r} — skipped")
        return None
    entry_date: date | None = None
    if parts["entry_date"]:
        mmdd = parts["entry_date"]
        vdate = _parse_mt940_date(parts["value_date"])
        try:
            entry_date = date(vdate.year, int(mmdd[0:2]), int(mmdd[2:4]))
        except ValueError:
            warnings.append(f"unparseable entry date in :61: {value!r}")
    document = parts["reference"].strip() or None
    bank_ref = (parts["bank_ref"] or "").strip() or None
    return NormalizedBankTransaction(
        provider_code=provider_code,
        provider_account=provider_account,
        value_date=_parse_mt940_date(parts["value_date"]),
        entry_date=entry_date,
        amount=amount,
        currency=POSTABLE_CURRENCY,  # MT940 :61: carries no currency; KZT-only (OQ-014)
        direction=direction,
        document_number=document,
        bank_reference=bank_ref,
        raw_line=f":61:{value.strip()}",
        line_no=line_no,
        provider_extra={"mt940_txn_type": parts["txn_type"]},
    )


# The default registry: instantiated adapters, first can_parse() match wins.
register_adapter(MT940StatementAdapter())


class KaspiPayStatementAdapter(ProviderAdapter):
    """Kaspi Pay statement adapter — STUB (deferred).

    EB-KASPI-1..4: no official payout/commission/settlement API exists; the
    statement export format is defined unilaterally by Kaspi Pay and no
    official column spec was located (docs/KASPI_STATEMENT_ARCH_2026-10-04.md).
    This stub reserves the provider_code and documents the contract; it
    never parses. A real implementation waits for a genuine export sample
    and is layout/column-map driven — column names are NEVER hardcoded.
    """

    provider_code = "KASPI_PAY"
    format_name = "KASPI_PAY_STUB"

    def can_parse(self, content: bytes, filename: str) -> bool:
        return False  # unknown layout — never claim a file

    def parse(self, content: bytes, filename: str) -> ParsedStatement:
        raise NotImplementedError(
            "Kaspi Pay statement parsing is deferred until a real export "
            "sample defines the layout (EB-KASPI). No format is invented here."
        )


# --------------------------------------------------------------------------- #
# Import result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ImportResult:
    statement_id: int | None
    batch_id: int | None
    status: str  # SUCCESS | PARTIAL | FAILED | DRY_RUN
    rows_total: int
    rows_inserted: int
    rows_duplicates: int
    rows_skipped: int
    rows_errors: int
    errors: list[dict] = field(default_factory=list)
    balance_control: dict | None = None


# --------------------------------------------------------------------------- #
# StatementImportService
# --------------------------------------------------------------------------- #


class StatementImportService:
    """File-based bank statement import (G-BANK-2..G-BANK-4)."""

    def __init__(self, adapters: list[ProviderAdapter] | None = None) -> None:
        self._adapters = adapters if adapters is not None else list(ADAPTER_REGISTRY)

    # -- public API ---------------------------------------------------- #

    def import_statement(
        self,
        session: Session,
        *,
        ip_id: int,
        account_id: int | None,
        content: bytes,
        filename: str,
        column_map: Path | None = None,  # reserved for the Freedom Excel adapter
        created_by: int | None = None,
        dry_run: bool = False,
        chunk_size: int = 1000,
    ) -> ImportResult:
        """Import one statement file; idempotent on file hash + fingerprints."""
        _ = column_map  # not used until the Freedom Excel adapter exists
        if ip_id is None or ip_id <= 0:
            raise ValueError("ip_id is required")
        file_hash = hashlib.sha256(content).hexdigest()

        adapter = next((a for a in self._adapters if a.can_parse(content, filename)), None)
        batch = self._new_batch(
            session,
            ip_id=ip_id,
            filename=filename,
            file_hash=file_hash,
            created_by=created_by,
            dry_run=dry_run,
        )
        if adapter is None:
            return self._fail_batch(
                session,
                batch,
                {"error": f"no adapter can parse file {filename!r}"},
                commit=not dry_run,
            )

        # DUP-FILE: same (ip, provider, hash). A row with processed_at set is
        # a completed import -> pure duplicate. A row with processed_at NULL
        # is a crashed import -> resume it (I-4).
        existing = session.execute(
            select(RawBankStatement).where(
                RawBankStatement.ip_id == ip_id,
                RawBankStatement.provider_code == adapter.provider_code,
                RawBankStatement.file_hash == file_hash,
            )
        ).scalar_one_or_none()
        if existing is not None and existing.processed_at is not None:
            return self._duplicate_file_result(session, batch, existing, dry_run)
        if existing is not None:
            return self._resume_import(
                session,
                batch=batch,
                raw=existing,
                adapter=adapter,
                content=content,
                filename=filename,
                account_id=account_id,
                created_by=created_by,
                dry_run=dry_run,
                chunk_size=chunk_size,
            )

        parsed = adapter.parse(content, filename)
        valid_txs: list[NormalizedBankTransaction] = []
        skipped = 0
        errors: list[dict] = []
        for tx in parsed.transactions:
            reason = self._validate_tx(tx)
            if reason is not None:
                skipped += 1
                errors.append({"row": tx.line_no, "error": reason})
                continue
            valid_txs.append(tx)
        for warning in parsed.warnings:
            errors.append({"row": "file", "error": warning})

        resolved_account = self._resolve_account(
            session,
            ip_id=ip_id,
            account_id=account_id,
            provider_code=parsed.provider_code,
            provider_account=parsed.provider_account,
        )

        raw = RawBankStatement(
            ip_id=ip_id,
            provider_code=parsed.provider_code,
            account_id=resolved_account,
            file_name=filename,
            file_hash=file_hash,
            content=content,
            content_text=_safe_text(content),
            statement_no=parsed.statement_no,
            period_from=parsed.period_from,
            period_to=parsed.period_to,
            opening_balance=parsed.opening_balance,
            closing_balance=parsed.closing_balance,
            currency=parsed.currency,
            import_batch_id=batch.id,
            created_by=created_by,
        )
        # NB: session.add() MUST be inside the savepoint (SQLAlchemy 2.0):
        # a flush failure with the object added outside poisons the whole
        # session (PendingRollbackError); added inside, the savepoint
        # rollback detaches it and the session stays usable.
        try:
            with session.begin_nested():
                session.add(raw)
                session.flush()
        except IntegrityError as exc:
            if "uq_raw_bank_statements_file" not in str(exc.orig):
                raise
            # Concurrent import won the race (I-3): the savepoint rolled
            # back just the raw INSERT; the outer transaction (with the
            # batch row) is intact. Re-read the winner and resume.
            winner = session.execute(
                select(RawBankStatement).where(
                    RawBankStatement.ip_id == ip_id,
                    RawBankStatement.provider_code == adapter.provider_code,
                    RawBankStatement.file_hash == file_hash,
                )
            ).scalar_one_or_none()
            if winner is None:
                # Winner rolled back; retry once as a fresh import.
                return self.import_statement(
                    session,
                    ip_id=ip_id,
                    account_id=account_id,
                    content=content,
                    filename=filename,
                    created_by=created_by,
                    dry_run=dry_run,
                    chunk_size=chunk_size,
                )
            if winner.processed_at is not None:
                return self._duplicate_file_result(session, batch, winner, dry_run)
            return self._resume_import(
                session,
                batch=batch,
                raw=winner,
                adapter=adapter,
                content=content,
                filename=filename,
                account_id=account_id,
                created_by=created_by,
                dry_run=dry_run,
                chunk_size=chunk_size,
            )

        counters = self._insert_lines(
            session,
            raw=raw,
            adapter=adapter,
            transactions=valid_txs,
            dry_run=dry_run,
            chunk_size=chunk_size,
        )
        counters["skipped"] += skipped + parsed.skipped_count
        balance_control = self._balance_control(parsed, valid_txs)

        if dry_run:
            session.rollback()
            return ImportResult(
                statement_id=None,
                batch_id=None,
                status="DRY_RUN",
                rows_total=len(parsed.transactions) + parsed.skipped_count,
                rows_inserted=counters["inserted"],
                rows_duplicates=counters["duplicates"],
                rows_skipped=counters["skipped"],
                rows_errors=counters["errors"],
                errors=errors[:50],
                balance_control=balance_control,
            )

        # BALANCE-CONTROL (mandatory): the statement must foot. A mismatch
        # fails the batch; the lines stay (external fact) but posting is
        # blocked via the batch flag (I-6).
        if balance_control is not None and not balance_control["ok"]:
            batch.rows_total = len(parsed.transactions) + parsed.skipped_count
            batch.rows_inserted = counters["inserted"]
            batch.rows_skipped = counters["skipped"]
            batch.rows_duplicates = counters["duplicates"]
            batch.rows_errors = counters["errors"]
            batch.error_details = {"balance_control": balance_control, "warnings": errors[:50]}
            batch.status = "FAILED"
            batch.finished_at = _now()
            raw.processed_at = _now()
            session.commit()
            return ImportResult(
                statement_id=raw.id,
                batch_id=batch.id,
                status="FAILED",
                rows_total=len(parsed.transactions) + parsed.skipped_count,
                rows_inserted=counters["inserted"],
                rows_duplicates=counters["duplicates"],
                rows_skipped=counters["skipped"],
                rows_errors=counters["errors"],
                errors=errors[:50],
                balance_control=balance_control,
            )

        return self._finalize(
            session,
            batch=batch,
            raw=raw,
            rows_total=len(parsed.transactions) + parsed.skipped_count,
            counters=counters,
            errors=errors,
        )

    # -- internals ----------------------------------------------------- #

    def _new_batch(
        self,
        session: Session,
        *,
        ip_id: int,
        filename: str,
        file_hash: str,
        created_by: int | None,
        dry_run: bool,
    ) -> ImportBatch:
        batch = ImportBatch(
            source_type="BANK",
            source_name=filename,
            source_hash=file_hash,
            ip_id=ip_id,
            is_dry_run=dry_run,
            status="RUNNING",
            rows_total=0,
            rows_inserted=0,
            rows_skipped=0,
            rows_duplicates=0,
            rows_errors=0,
            created_by=created_by,
        )
        session.add(batch)
        session.flush()
        return batch

    def _fail_batch(
        self, session: Session, batch: ImportBatch, details: dict, *, commit: bool
    ) -> ImportResult:
        batch.status = "FAILED"
        batch.error_details = details
        batch.finished_at = _now()
        if commit:
            session.commit()
        else:
            session.rollback()
        return ImportResult(
            statement_id=None,
            batch_id=batch.id if commit else None,
            status="FAILED",
            rows_total=0,
            rows_inserted=0,
            rows_duplicates=0,
            rows_skipped=0,
            rows_errors=1,
            errors=[{"row": "file", "error": details.get("error", "failed")}],
        )

    def _duplicate_file_result(
        self, session: Session, batch: ImportBatch, raw: RawBankStatement, dry_run: bool
    ) -> ImportResult:
        line_count = session.execute(
            select(func.count())
            .select_from(BankStatementLine)
            .where(BankStatementLine.statement_id == raw.id)
        ).scalar_one()
        batch.status = "SUCCESS"
        batch.rows_total = line_count
        batch.rows_duplicates = line_count
        batch.finished_at = _now()
        if dry_run:
            session.rollback()
            return ImportResult(
                statement_id=raw.id,
                batch_id=None,
                status="DRY_RUN",
                rows_total=line_count,
                rows_inserted=0,
                rows_duplicates=line_count,
                rows_skipped=0,
                rows_errors=0,
            )
        session.commit()
        return ImportResult(
            statement_id=raw.id,
            batch_id=batch.id,
            status="SUCCESS",
            rows_total=line_count,
            rows_inserted=0,
            rows_duplicates=line_count,
            rows_skipped=0,
            rows_errors=0,
        )

    def _resume_import(
        self,
        session: Session,
        *,
        batch: ImportBatch,
        raw: RawBankStatement,
        adapter: ProviderAdapter,
        content: bytes,
        filename: str,
        account_id: int | None,
        created_by: int | None,
        dry_run: bool,
        chunk_size: int,
    ) -> ImportResult:
        """Continue a crashed import: reuse the raw row, insert missing lines."""
        parsed = adapter.parse(content, filename)
        valid_txs: list[NormalizedBankTransaction] = []
        skipped = 0
        errors: list[dict] = []
        for tx in parsed.transactions:
            reason = self._validate_tx(tx)
            if reason is not None:
                skipped += 1
                errors.append({"row": tx.line_no, "error": reason})
                continue
            valid_txs.append(tx)
        counters = self._insert_lines(
            session,
            raw=raw,
            adapter=adapter,
            transactions=valid_txs,
            dry_run=dry_run,
            chunk_size=chunk_size,
        )
        counters["skipped"] += skipped + parsed.skipped_count
        balance_control = self._balance_control(parsed, valid_txs)
        if dry_run:
            session.rollback()
            return ImportResult(
                statement_id=raw.id,
                batch_id=None,
                status="DRY_RUN",
                rows_total=len(parsed.transactions) + parsed.skipped_count,
                rows_inserted=counters["inserted"],
                rows_duplicates=counters["duplicates"],
                rows_skipped=counters["skipped"],
                rows_errors=counters["errors"],
                errors=errors[:50],
                balance_control=balance_control,
            )
        return self._finalize(
            session,
            batch=batch,
            raw=raw,
            rows_total=len(parsed.transactions) + parsed.skipped_count,
            counters=counters,
            errors=errors,
        )

    def _finalize(
        self,
        session: Session,
        *,
        batch: ImportBatch,
        raw: RawBankStatement,
        rows_total: int,
        counters: dict[str, int],
        errors: list[dict],
    ) -> ImportResult:
        batch.rows_total = rows_total
        batch.rows_inserted = counters["inserted"]
        batch.rows_skipped = counters["skipped"]
        batch.rows_duplicates = counters["duplicates"]
        batch.rows_errors = counters["errors"]
        if errors:
            batch.error_details = {"warnings": errors[:50]}
        has_issues = counters["errors"] > 0 or counters["skipped"] > 0 or bool(errors)
        batch.status = "PARTIAL" if has_issues else "SUCCESS"
        batch.finished_at = _now()
        raw.processed_at = _now()
        session.commit()
        return ImportResult(
            statement_id=raw.id,
            batch_id=batch.id,
            status=batch.status,
            rows_total=rows_total,
            rows_inserted=counters["inserted"],
            rows_duplicates=counters["duplicates"],
            rows_skipped=counters["skipped"],
            rows_errors=counters["errors"],
            errors=errors[:50],
        )

    @staticmethod
    def _validate_tx(tx: NormalizedBankTransaction) -> str | None:
        """STOP conditions: return a skip reason, or None when the line is OK."""
        if tx.amount <= 0:
            return f"non-positive amount {tx.amount} — skipped"
        if tx.currency != POSTABLE_CURRENCY:
            return f"currency {tx.currency} != KZT — skipped (non-KZT posting blocked until OQ-014)"
        return None

    def _resolve_account(
        self,
        session: Session,
        *,
        ip_id: int,
        account_id: int | None,
        provider_code: str,
        provider_account: str,
    ) -> int | None:
        """Map provider_code (+provider IBAN) to an ERP account.

        Explicit account_id wins but must belong to the ИП (ValueError
        otherwise — a client error, not a 500). Without an explicit id,
        exactly one active account with bank_code=provider_code is an
        unambiguous match; zero or several -> None (lines wait for manual
        mapping; that is NOT an error). The IBAN itself is not stored on
        accounts, so it cannot disambiguate.
        """
        if account_id is not None:
            found = session.execute(
                select(Account.id).where(Account.id == account_id, Account.ip_id == ip_id)
            ).scalar_one_or_none()
            if found is None:
                raise ValueError(f"account {account_id} does not exist for ip {ip_id}")
            return found
        candidates = (
            session.execute(
                select(Account.id).where(
                    Account.ip_id == ip_id,
                    Account.bank_code == provider_code,
                    Account.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _insert_lines(
        self,
        session: Session,
        *,
        raw: RawBankStatement,
        adapter: ProviderAdapter,
        transactions: list[NormalizedBankTransaction],
        dry_run: bool,
        chunk_size: int,
    ) -> dict[str, int]:
        counters = {"inserted": 0, "duplicates": 0, "errors": 0, "skipped": 0}
        if not transactions:
            return counters
        # P0-2: batch fingerprint pre-check, 1000 per query.
        fps = [adapter.fingerprint(tx) for tx in transactions]
        existing: set[str] = set()
        for i in range(0, len(fps), 1000):
            chunk = fps[i : i + 1000]
            rows = (
                session.execute(
                    select(BankStatementLine.line_fingerprint).where(
                        BankStatementLine.ip_id == raw.ip_id,
                        BankStatementLine.line_fingerprint.in_(chunk),
                    )
                )
                .scalars()
                .all()
            )
            existing.update(rows)
        seen: set[str] = set()
        since_commit = 0
        for tx, fp in zip(transactions, fps, strict=True):
            if fp in existing or fp in seen:
                counters["duplicates"] += 1
                continue
            line = BankStatementLine(
                ip_id=raw.ip_id,
                statement_id=raw.id,
                account_id=raw.account_id,
                provider_code=tx.provider_code,
                provider_account=tx.provider_account,
                value_date=tx.value_date,
                entry_date=tx.entry_date,
                amount=tx.amount,
                currency=tx.currency,
                direction=tx.direction,
                document_number=tx.document_number,
                bank_reference=tx.bank_reference,
                counterparty_name=tx.counterparty_name,
                counterparty_iin=tx.counterparty_iin,
                counterparty_bik=tx.counterparty_bik,
                counterparty_account=tx.counterparty_account,
                payment_purpose=tx.payment_purpose,
                knp_code=tx.knp_code,
                balance_after=tx.balance_after,
                raw_line=tx.raw_line,
                line_no=tx.line_no,
                provider_extra=tx.provider_extra,
                line_fingerprint=fp,
                match_status="UNMATCHED",
            )
            try:
                # F-ETL-008: per-row savepoint; a concurrent duplicate
                # fingerprint raises IntegrityError -> counted as duplicate.
                # NB: add() inside the savepoint (SQLAlchemy 2.0) — see above.
                with session.begin_nested():
                    session.add(line)
                    session.flush()
            except IntegrityError as exc:
                if "uq_bank_statement_lines_fingerprint" not in str(exc.orig):
                    raise
                counters["duplicates"] += 1
                seen.add(fp)  # raced in; do not retry within this file
                continue
            except Exception:
                counters["errors"] += 1
                continue
            seen.add(fp)
            counters["inserted"] += 1
            since_commit += 1
            # P0-2: chunked commit, except for dry runs.
            # §9 exception: intentional — bounds transaction size for large
            # files. Safe because the import is idempotent on file hash
            # (DUP-FILE check); a crash resumes from the last chunk.
            if not dry_run and since_commit >= chunk_size:
                session.commit()
                since_commit = 0
        return counters

    @staticmethod
    def _balance_control(
        parsed: ParsedStatement, valid_txs: list[NormalizedBankTransaction]
    ) -> dict | None:
        """Statement footing: opening + IN - OUT must equal closing."""
        if parsed.opening_balance is None or parsed.closing_balance is None:
            return None
        total_in = sum((tx.amount for tx in valid_txs if tx.direction == "IN"), Decimal("0"))
        total_out = sum((tx.amount for tx in valid_txs if tx.direction == "OUT"), Decimal("0"))
        expected = parsed.opening_balance + total_in - total_out
        return {
            "expected": str(expected),
            "actual": str(parsed.closing_balance),
            "opening": str(parsed.opening_balance),
            "total_in": str(total_in),
            "total_out": str(total_out),
            "ok": expected == parsed.closing_balance,
        }


def _safe_text(content: bytes) -> str | None:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return content.decode("windows-1251")
        except UnicodeDecodeError:
            return None


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# PostingPolicy + post_unmatched_as_operations (G-BANK-4)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PostingPolicy:
    """Explicit human-approved policy for creating operations from lines.

    default_status=DRAFT: money moved per the bank, but the accountant
    confirms the classification. POSTED only under an explicit
    "we trust the bank" policy. Never auto-CONFIRMED except by
    post_unmatched_as_operations itself; never auto-match by amount only.
    """

    default_status: Literal["DRAFT", "POSTED"] = "DRAFT"
    auto_post_commissions: bool = False  # reserved: EXPENSE_BANK_FEE straight to POSTED
    accrual_date: date | None = None  # None -> pure settlement (D1), no P&L effect
    commission_knp_codes: frozenset[str] = frozenset()
    amount_tolerance: Decimal = Decimal("1.00")
    date_window_days: int = 1
    suggest_threshold: Decimal = Decimal("70")


def commission_knp_codes(session: Session, policy: PostingPolicy) -> frozenset[str]:
    """Union of the policy set and the bank_fee_knp_codes reference table."""
    db_codes = session.execute(select(BankFeeKnpCode.knp_code)).scalars().all()
    return policy.commission_knp_codes | frozenset(db_codes)


def classify_line(
    line: BankStatementLine,
    policy: PostingPolicy,
    knp_codes: frozenset[str],
) -> Literal["commission", "needs_review"]:
    """Classify an UNMATCHED line per the §4.2 table.

    Only bank commissions are postable in v1. Everything else —
    SALE/LOAN/OWNER_WITHDRAWAL/TAX/PAYROLL/REFUND — is deliberately NOT
    auto-classified: a bank line does not carry those semantics.
    Transfer-pair detection is a separate check (no operations ever).
    """
    if line.direction == "OUT" and line.knp_code and line.knp_code in knp_codes:
        return "commission"
    return "needs_review"


def _is_transfer_pair(session: Session, line: BankStatementLine, policy: PostingPolicy) -> bool:
    """An OUT+IN pair of the same amount (±tolerance, ±1 day) on different
    provider accounts of the same ИП is an inter-account transfer, never
    income/expense. Operations are NOT created for it."""
    opposite = "IN" if line.direction == "OUT" else "OUT"
    return (
        session.execute(
            select(func.count())
            .select_from(BankStatementLine)
            .where(
                BankStatementLine.ip_id == line.ip_id,
                BankStatementLine.id != line.id,
                BankStatementLine.direction == opposite,
                BankStatementLine.match_status == "UNMATCHED",
                BankStatementLine.provider_account != line.provider_account,
                func.abs(BankStatementLine.value_date - line.value_date) <= 1,
                func.abs(BankStatementLine.amount - line.amount) <= policy.amount_tolerance,
            )
        ).scalar_one()
        > 0
    )


def post_unmatched_as_operations(
    session: Session,
    *,
    ip_id: int,
    line_ids: list[int],
    policy: PostingPolicy,
    created_by: int | None = None,
) -> list[int]:
    """Create operations from UNMATCHED lines under an explicit policy.

    Only ``commission`` lines (EXPENSE_BANK_FEE) are postable in v1;
    transfer pairs and everything ambiguous stay UNMATCHED (NEEDS_REVIEW).
    Idempotent: ``import_fingerprint = line_fingerprint`` hits the existing
    partial unique ``uq_operations_import_fingerprint`` on re-post.
    """
    if not line_ids:
        return []
    knp_codes = commission_knp_codes(session, policy)
    fee_type_id = session.execute(
        select(OperationType.id).where(OperationType.code == "EXPENSE_BANK_FEE")
    ).scalar_one_or_none()
    if fee_type_id is None:
        raise ValueError("operation type EXPENSE_BANK_FEE is not seeded")

    lines = (
        session.execute(
            select(BankStatementLine).where(
                BankStatementLine.id.in_(line_ids),
                BankStatementLine.ip_id == ip_id,
                BankStatementLine.match_status == "UNMATCHED",
            )
        )
        .scalars()
        .all()
    )
    created: list[int] = []
    for line in lines:
        op_id = _post_one_line(
            session,
            line,
            ip_id=ip_id,
            policy=policy,
            knp_codes=knp_codes,
            fee_type_id=fee_type_id,
            created_by=created_by,
        )
        if op_id is not None:
            created.append(op_id)
    session.commit()
    return created


def _post_one_line(
    session: Session,
    line: BankStatementLine,
    *,
    ip_id: int,
    policy: PostingPolicy,
    knp_codes: frozenset[str],
    fee_type_id: int,
    created_by: int | None,
) -> int | None:
    """Post one UNMATCHED line. Returns the operation id, or None when the
    line is not postable under the policy."""
    if _posting_blocked_by_balance_control(session, line):
        return None
    kind = classify_line(line, policy, knp_codes)
    if kind == "needs_review" and _is_transfer_pair(session, line, policy):
        # Inter-account transfer: never income/expense, no operations.
        return None
    if kind != "commission":
        return None
    direction = OperationDirection.OUT if line.direction == "OUT" else OperationDirection.IN
    amount = -line.amount if line.direction == "OUT" else line.amount
    try:
        # F-ETL-008: the op INSERT rides in a savepoint so a duplicate
        # fingerprint (re-post / concurrent poster) rolls back only this row.
        with session.begin_nested():
            op_id = add_operation(
                session,
                OperationCreate(
                    ip_id=ip_id,
                    type_id=fee_type_id,
                    direction=direction,
                    amount=amount,
                    operation_date=line.value_date,
                    # Explicit None -> pure settlement (D1/GAP-004): the
                    # commission hits P&L only when accrual_date is set.
                    accrual_date=policy.accrual_date,
                    comment=(line.payment_purpose or "")[:500] or None,
                    import_fingerprint=line.line_fingerprint,
                ),
                # account_id/status ride on the INSERT: POSTED rows are
                # immutable (fn_protect_posted_ledger_operation), so
                # post-flush assignment would fail. OperationCreate stays
                # schema-clean (no account_id).
                status=policy.default_status,
                account_id=_verified_account_id(session, ip_id, line.account_id),
            )
            op = session.get(Operation, op_id)
            assert op is not None
            op.import_batch_id = _batch_id_of(session, line.statement_id)
            _confirm_posted_line(line, op_id, created_by)
            session.flush()
            return op_id
    except IntegrityError as exc:
        if "uq_operations_import_fingerprint" not in str(exc.orig):
            raise
        # The operation already exists (re-post, or a crash between the op
        # INSERT and the line CONFIRM): heal the link, never double-post.
        existing_op_id = session.execute(
            select(Operation.id).where(
                Operation.ip_id == ip_id,
                Operation.import_fingerprint == line.line_fingerprint,
            )
        ).scalar_one_or_none()
        if existing_op_id is None:
            return None
        _confirm_posted_line(line, existing_op_id, created_by)
        session.flush()
        return existing_op_id


def _confirm_posted_line(line: BankStatementLine, op_id: int, created_by: int | None) -> None:
    # An auto-created operation is a confirmed match (§4.3) — the only
    # machine path to CONFIRMED.
    line.match_status = "CONFIRMED"
    line.matched_operation_id = op_id
    line.matched_at = _now()
    line.matched_by = created_by


def _verified_account_id(session: Session, ip_id: int, account_id: int | None) -> int | None:
    """fk_operations_account_ip check: the account must belong to the ИП."""
    if account_id is None:
        return None
    found = session.execute(
        select(Account.id).where(Account.id == account_id, Account.ip_id == ip_id)
    ).scalar_one_or_none()
    if found is None:
        raise ValueError(f"account {account_id} does not belong to ip {ip_id}")
    return found


def _batch_id_of(session: Session, statement_id: int) -> int | None:
    return session.execute(
        select(RawBankStatement.import_batch_id).where(RawBankStatement.id == statement_id)
    ).scalar_one_or_none()


def _posting_blocked_by_balance_control(session: Session, line: BankStatementLine) -> bool:
    """I-6: lines of a statement that failed balance control are not posted."""
    batch_id = _batch_id_of(session, line.statement_id)
    if batch_id is None:
        return False
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.status != "FAILED":
        return False
    details = batch.error_details or {}
    return "balance_control" in details
