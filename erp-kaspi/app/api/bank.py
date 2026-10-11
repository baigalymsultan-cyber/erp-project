"""Bank statement import + reconciliation routes (G-BANK-2..5)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.policy import ensure_ip_access, ensure_ip_access_or_all
from app.core.security import require_privilege
from app.models.bank import BankStatementLine, RawBankStatement
from app.schemas.bank import (
    BankLineConfirmRequest,
    BankLineIgnoreRequest,
    BankLinesPostRequest,
    BankLinesSuggestRequest,
)

router = APIRouter()


def _serialize_bank_line(line: BankStatementLine) -> dict[str, object]:
    audit = (line.provider_extra or {}).get("audit") or {}
    return {
        "id": line.id,
        "statement_id": line.statement_id,
        "account_id": line.account_id,
        "provider_code": line.provider_code,
        "provider_account": line.provider_account,
        "value_date": line.value_date.isoformat(),
        "entry_date": line.entry_date.isoformat() if line.entry_date else None,
        "amount": str(line.amount),
        "currency": line.currency,
        "direction": line.direction,
        "document_number": line.document_number,
        "bank_reference": line.bank_reference,
        "counterparty_name": line.counterparty_name,
        "counterparty_iin": line.counterparty_iin,
        "payment_purpose": line.payment_purpose,
        "knp_code": line.knp_code,
        "match_status": line.match_status,
        "matched_operation_id": line.matched_operation_id,
        "verdict": audit.get("verdict"),
    }


@router.get("/bank/statements", tags=["bank"])
def list_bank_statements(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    ip_id: int | None = Query(default=None),
    provider_code: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """List imported bank statement files (G-BANK-2)."""
    from app.services.bank_import import PROVIDER_CODES

    allowed = ensure_ip_access_or_all(session, principal)
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        if allowed is not None:
            allowed = [ip_id]
    if provider_code is not None and provider_code not in PROVIDER_CODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown provider_code")

    stmt = select(RawBankStatement)
    if ip_id is not None:
        stmt = stmt.where(RawBankStatement.ip_id == ip_id)
    if provider_code is not None:
        stmt = stmt.where(RawBankStatement.provider_code == provider_code)
    if allowed is not None:
        stmt = stmt.where(RawBankStatement.ip_id.in_(allowed))

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        session.execute(stmt.order_by(RawBankStatement.id.desc()).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": s.id,
                "ip_id": s.ip_id,
                "provider_code": s.provider_code,
                "account_id": s.account_id,
                "file_name": s.file_name,
                "statement_no": s.statement_no,
                "period_from": s.period_from.isoformat() if s.period_from else None,
                "period_to": s.period_to.isoformat() if s.period_to else None,
                "opening_balance": (
                    str(s.opening_balance) if s.opening_balance is not None else None
                ),
                "closing_balance": (
                    str(s.closing_balance) if s.closing_balance is not None else None
                ),
                "currency": s.currency,
                "received_at": s.received_at.isoformat() if s.received_at else None,
                "processed_at": s.processed_at.isoformat() if s.processed_at else None,
            }
            for s in rows
        ],
    }


@router.post("/bank/statements/import", tags=["bank"])
def import_bank_statement(
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
    file: UploadFile = File(...),
    ip_id: int = Form(...),
    account_id: int | None = Form(default=None),
    dry_run: bool = Form(default=False),
) -> dict[str, object]:
    """Import a bank statement file (MT940). Idempotent on file hash (G-BANK-3)."""
    from app.services.bank_import import StatementImportService

    ensure_ip_access(session, principal, ip_id)
    # §5 DoS guard — bounded read: never load more than MAX_STATEMENT_BYTES
    # into RAM. Chunked so a malicious Content-Length can't OOM the worker.
    from app.services.bank_import import MAX_STATEMENT_BYTES

    chunks: list[bytes] = []
    total = 0
    while True:
        piece = file.file.read(1024 * 1024)  # 1 MiB chunks
        if not piece:
            break
        total += len(piece)
        if total > MAX_STATEMENT_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"file exceeds {MAX_STATEMENT_BYTES // (1024 * 1024)} MiB limit",
            )
        chunks.append(piece)
    content = b"".join(chunks)
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty file")
    user_id = principal.get("user_id")
    service = StatementImportService()
    try:
        result = service.import_statement(
            session,
            ip_id=ip_id,
            account_id=account_id,
            content=content,
            filename=file.filename or "statement",
            created_by=int(user_id) if isinstance(user_id, int) else None,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {
        "statement_id": result.statement_id,
        "batch_id": result.batch_id,
        "status": result.status,
        "rows_total": result.rows_total,
        "rows_inserted": result.rows_inserted,
        "rows_duplicates": result.rows_duplicates,
        "rows_skipped": result.rows_skipped,
        "rows_errors": result.rows_errors,
        "errors": result.errors,
        "balance_control": result.balance_control,
    }


@router.get("/bank/statements/{statement_id}/lines", tags=["bank"])
def list_bank_statement_lines(
    statement_id: int,
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    match_status: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """List normalized lines of one statement (G-BANK-3)."""
    statement = session.get(RawBankStatement, statement_id)
    if statement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "statement not found")
    ensure_ip_access(session, principal, statement.ip_id)
    if match_status is not None and match_status not in (
        "UNMATCHED",
        "SUGGESTED",
        "CONFIRMED",
        "IGNORED",
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown match_status")

    stmt = select(BankStatementLine).where(BankStatementLine.statement_id == statement_id)
    if match_status is not None:
        stmt = stmt.where(BankStatementLine.match_status == match_status)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        session.execute(stmt.order_by(BankStatementLine.id).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_serialize_bank_line(line) for line in rows],
    }


@router.post("/bank/lines/suggest-matches", tags=["bank"])
def suggest_bank_line_matches(
    data: BankLinesSuggestRequest,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
    ip_id: int = Query(...),
) -> dict[str, object]:
    """Run matcher v1 (R1..R4) over UNMATCHED lines. Suggestions only — the
    machine never CONFIRMs (G-BANK-5)."""
    from app.services.reconciliation import BankReconciliationService

    ensure_ip_access(session, principal, ip_id)
    if data.statement_id is not None:
        statement = session.get(RawBankStatement, data.statement_id)
        if statement is None or statement.ip_id != ip_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "statement not found")
    service = BankReconciliationService()
    counts = service.suggest_matches(
        session,
        ip_id=ip_id,
        statement_id=data.statement_id,
        account_id=data.account_id,
    )
    return {"ip_id": ip_id, **counts}


@router.post("/bank/lines/{line_id}/confirm", tags=["bank"])
def confirm_bank_line(
    line_id: int,
    data: BankLineConfirmRequest,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Human confirm: link a line to a POSTED operation (G-BANK-5)."""
    from app.services.reconciliation import BankReconciliationService

    line = session.get(BankStatementLine, line_id)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "line not found")
    ensure_ip_access(session, principal, line.ip_id)
    service = BankReconciliationService()
    user_id = principal.get("user_id")
    try:
        confirmed = service.confirm_line(
            session,
            ip_id=line.ip_id,
            line_id=line_id,
            operation_id=data.operation_id,
            user_id=int(user_id) if isinstance(user_id, int) else None,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return _serialize_bank_line(confirmed)


@router.post("/bank/lines/{line_id}/ignore", tags=["bank"])
def ignore_bank_line(
    line_id: int,
    data: BankLineIgnoreRequest,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Human ignore with a mandatory reason (recorded in the audit trail)."""
    from app.services.reconciliation import BankReconciliationService

    line = session.get(BankStatementLine, line_id)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "line not found")
    ensure_ip_access(session, principal, line.ip_id)
    service = BankReconciliationService()
    user_id = principal.get("user_id")
    try:
        ignored = service.ignore_line(
            session,
            ip_id=line.ip_id,
            line_id=line_id,
            reason=data.reason,
            user_id=int(user_id) if isinstance(user_id, int) else None,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return _serialize_bank_line(ignored)


@router.post("/bank/lines/post", tags=["bank"])
def post_bank_lines(
    data: BankLinesPostRequest,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
    ip_id: int = Query(...),
) -> dict[str, object]:
    """Create operations from UNMATCHED lines under an explicit PostingPolicy.

    Only bank commissions (EXPENSE_BANK_FEE) are postable in v1; transfer
    pairs and ambiguous lines stay UNMATCHED (G-BANK-4).
    """
    from decimal import Decimal as _Decimal

    from app.services.bank_import import PostingPolicy, post_unmatched_as_operations

    ensure_ip_access(session, principal, ip_id)
    policy = PostingPolicy(
        default_status=data.policy.default_status,
        accrual_date=data.policy.accrual_date,
        commission_knp_codes=frozenset(data.policy.commission_knp_codes),
        amount_tolerance=_Decimal(str(data.policy.amount_tolerance)),
        date_window_days=data.policy.date_window_days,
    )
    try:
        created = post_unmatched_as_operations(
            session,
            ip_id=ip_id,
            line_ids=data.line_ids,
            policy=policy,
            created_by=(
                int(principal["user_id"]) if isinstance(principal.get("user_id"), int) else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {"ip_id": ip_id, "created_operation_ids": created}
