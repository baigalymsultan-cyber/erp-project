"""Ledger operation routes — §16 (append-only financial records)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.policy import assert_ip_exists, ensure_ip_access, ensure_ip_access_or_all
from app.core.security import require_privilege
from app.models.finance import Operation
from app.models.reference import Counterparty, IpEntity, OperationType
from app.schemas.finance import OperationCreate
from app.services import add_operation, reverse_operation

router = APIRouter()


@router.post("/operations/{operation_id}/reverse", tags=["operations"])
def post_reverse_operation(
    operation_id: int,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    reversal_reason: str,
    amount: Decimal | None = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=64)] = None,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """ADR-016 — reversal: creates a compensating POSTED operation.

    The original operation is never mutated (no status flip, no comment
    overwrite). ``reversal_reason`` is mandatory (Q5). ``amount`` enables
    partial reversal (Q4); omitted = full reversal. ``Idempotency-Key``
    header makes retries safe.
    """
    target = session.get(Operation, operation_id)
    if target is None:
        raise HTTPException(status_code=404, detail="operation not found")
    ensure_ip_access(session, principal, target.ip_id)
    try:
        result = reverse_operation(
            session,
            operation_id,
            reversal_reason,
            amount=amount,
            idempotency_key=idempotency_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return result


@router.post("/operations", status_code=status.HTTP_201_CREATED, tags=["ledger"])
def post_operation(
    data: OperationCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=64)] = None,
) -> dict[str, object]:
    ensure_ip_access(session, principal, data.ip_id)
    assert_ip_exists(session, data.ip_id)
    # C-1: optional Idempotency-Key header for safe retries. Maps to
    # import_fingerprint (covered by uq_operations_import_fingerprint).
    # Check first (like add_transfer): if the key was already used,
    # return the existing operation instead of creating a duplicate.
    fingerprint = data.import_fingerprint
    if idempotency_key and not fingerprint:
        fingerprint = idempotency_key.strip()
    if fingerprint:
        # P1-1/M-1: scope the replay lookup to the request's IP. A global
        # lookup would let a restricted principal learn operation IDs from
        # other IPs by guessing fingerprints (cross-IP information leak).
        existing_id = session.scalar(
            select(Operation.id).where(
                Operation.import_fingerprint == fingerprint,
                Operation.ip_id == data.ip_id,
            )
        )
        if existing_id is not None:
            return {"id": existing_id, "status": "IDEMPOTENT_REPLAY"}
        data = data.model_copy(update={"import_fingerprint": fingerprint})
    # P2-1: race-safe insert. Two concurrent requests with the same key can
    # both pass the check above; the loser hits uq_operations_import_fingerprint.
    # Use a SAVEPOINT so the IntegrityError doesn't poison the outer transaction;
    # on conflict, return the winner's row instead of HTTP 500.
    op_id: int | None = None
    try:
        with session.begin_nested():
            op_id = add_operation(session, data)
    except IntegrityError as exc:
        if not (fingerprint and "uq_operations_import_fingerprint" in str(exc.orig or exc)):
            raise
        existing_id = session.scalar(
            select(Operation.id).where(
                Operation.import_fingerprint == fingerprint,
                Operation.ip_id == data.ip_id,
            )
        )
        if existing_id is not None:
            return {"id": existing_id, "status": "IDEMPOTENT_REPLAY"}
        raise
    assert op_id is not None
    return {"id": op_id, "status": "APPEND_ONLY"}


@router.get("/operations", tags=["ledger"])
def list_operations(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    period_start: date | None = Query(default=None),
    period_end: date | None = Query(default=None),
    kind: Literal["INCOME", "EXPENSE", "TRANSFER"] | None = Query(default=None),
    ip_id: int | None = Query(default=None),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E1 — filtered operations list (replaces UI read-SQL A10/B5).

    REQ-TD016-02: the ИП allow-list is applied server-side via
    ensure_ip_access_or_all(); a restricted principal can never widen the
    filter beyond their allow-list. Sums are Decimal-safe strings (§94).
    """
    allowed = ensure_ip_access_or_all(session, principal)
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        if allowed is not None:
            allowed = [ip_id]
    like = f"%{(q or '').strip()}%"

    stmt = (
        select(
            Operation.id,
            Operation.operation_date,
            Operation.amount,
            Operation.comment,
            Operation.status,
            Operation.ip_id,
            IpEntity.name.label("ip_name"),
            OperationType.name.label("type_name"),
            OperationType.kind,
            Counterparty.name.label("counterparty_name"),
        )
        .join(OperationType, OperationType.id == Operation.type_id)
        .join(IpEntity, IpEntity.id == Operation.ip_id)
        .outerjoin(Counterparty, Counterparty.id == Operation.counterparty_id)
    )
    if period_start is not None:
        stmt = stmt.where(Operation.operation_date >= period_start)
    if period_end is not None:
        stmt = stmt.where(Operation.operation_date < period_end + timedelta(days=1))
    if kind is not None:
        stmt = stmt.where(OperationType.kind == kind)
    if ip_id is not None:
        stmt = stmt.where(Operation.ip_id == ip_id)
    if like != "%%":
        stmt = stmt.where(
            or_(
                Operation.comment.ilike(like),
                Counterparty.name.ilike(like),
                OperationType.name.ilike(like),
            )
        )
    if allowed is not None:
        stmt = stmt.where(Operation.ip_id.in_(allowed))

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(stmt.order_by(Operation.id.desc()).limit(limit).offset(offset)).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": r.id,
                "operation_date": r.operation_date.isoformat(),
                "type_name": r.type_name,
                "kind": r.kind,
                # §27 — expense amounts keep their negative storage sign.
                "amount": str(r.amount),
                "status": r.status,
                "ip_id": r.ip_id,
                "ip_name": r.ip_name,
                "counterparty": r.counterparty_name,
                "comment": r.comment,
            }
            for r in rows
        ],
    }


@router.get("/operations/daily", tags=["finance"])
def operations_daily(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    date_from: date = Query(...),
    date_to: date = Query(...),
) -> dict[str, object]:
    """TD-016 E2 — the daily POSTED-only income/expense series (A2/REQ-TD016-08).

    Identical aggregation to the former UI pnl_dataframe: POSTED-only,
    §27 signs preserved (expense is negative), day = date(operation_date).
    TD-016 REQ-TD016-02 (v8): the series is additionally scoped to the
    caller's ИП allow-list — the UI used to show business-wide totals to a
    restricted user, which leaked cross-ИП amounts (OWASP API1). Strictly
    stronger than the former UI behaviour.
    """
    allowed = ensure_ip_access_or_all(session, principal)
    params: dict[str, object] = {"start": date_from, "end": date_to}
    ip_filter = ""
    if allowed is not None:
        ip_filter = " AND o.ip_id = ANY(:allowed)"
        params["allowed"] = allowed
    rows = session.execute(
        text(
            "SELECT o.accrual_date::date AS day,"
            " SUM(CASE WHEN ot.kind = 'INCOME' THEN o.amount ELSE 0 END) AS income,"
            " SUM(CASE WHEN ot.kind = 'EXPENSE' THEN o.amount ELSE 0 END) AS expense"
            " FROM operations o"
            " JOIN operation_types ot ON ot.id = o.type_id"
            " WHERE o.accrual_date IS NOT NULL"
            " AND o.accrual_date BETWEEN :start AND :end"
            " AND o.status = 'POSTED'" + ip_filter + " GROUP BY o.accrual_date::date ORDER BY day"
        ),
        params,
    ).all()
    return {
        "items": [
            {"day": r.day.isoformat(), "income": str(r.income), "expense": str(r.expense)}
            for r in rows
        ]
    }


@router.get("/operations/summary", tags=["ledger"])
def operations_summary(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    period_start: date | None = Query(default=None),
    period_end: date | None = Query(default=None),
    kind: Literal["INCOME", "EXPENSE", "TRANSFER"] | None = Query(default=None),
    ip_id: int | None = Query(default=None),
    q: str = Query(default="", max_length=200),
) -> dict[str, object]:
    """TD-016 E18 — KPI aggregate for UI 'Операции' (replaces A10 KPI SQL).

    Filters match E1 and the detail endpoint. Income/expense totals include
    POSTED rows only; transfers are counted separately and never affect either
    total. ``count`` and ``status_counts`` cover every matching detail row.
    """
    allowed = ensure_ip_access_or_all(session, principal)
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        if allowed is not None:
            allowed = [ip_id]
    like = f"%{(q or '').strip()}%"

    stmt = (
        select(
            Operation.status,
            OperationType.kind,
            func.count(Operation.id).label("operation_count"),
            func.coalesce(func.sum(Operation.amount), 0).label("amount_sum"),
        )
        .join(OperationType, OperationType.id == Operation.type_id)
        .outerjoin(Counterparty, Counterparty.id == Operation.counterparty_id)
    )
    if period_start is not None:
        stmt = stmt.where(Operation.operation_date >= period_start)
    if period_end is not None:
        stmt = stmt.where(Operation.operation_date < period_end + timedelta(days=1))
    if kind is not None:
        stmt = stmt.where(OperationType.kind == kind)
    if ip_id is not None:
        stmt = stmt.where(Operation.ip_id == ip_id)
    if like != "%%":
        stmt = stmt.where(
            or_(
                Operation.comment.ilike(like),
                Counterparty.name.ilike(like),
                OperationType.name.ilike(like),
            )
        )
    if allowed is not None:
        stmt = stmt.where(Operation.ip_id.in_(allowed))

    rows = session.execute(stmt.group_by(Operation.status, OperationType.kind)).all()
    status_counts = {"POSTED": 0, "DRAFT": 0, "REVERSED": 0, "CORRECTED": 0}
    income = Decimal("0.00")
    expense = Decimal("0.00")
    posted_count = 0
    transfer_count = 0
    for row in rows:
        operation_count = int(row.operation_count)
        status_counts[row.status] += operation_count
        if row.status != "POSTED":
            continue
        posted_count += operation_count
        amount_sum = Decimal(row.amount_sum)
        if row.kind == "INCOME":
            income += amount_sum
        elif row.kind == "EXPENSE":
            expense -= amount_sum
        else:
            transfer_count += operation_count
    return {
        "income": str(income.quantize(Decimal("0.01"))),
        "expense": str(expense.quantize(Decimal("0.01"))),
        "count": sum(status_counts.values()),
        "posted_count": posted_count,
        "transfer_count": transfer_count,
        "status_counts": status_counts,
    }


@router.get("/operations/{op_id}", tags=["ledger"])
def get_operation(
    op_id: int,
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    # P1-2: avoid the 404/403 existence oracle. For IP-restricted principals,
    # scope the lookup to their allow-list so out-of-scope objects return 404
    # (indistinguishable from missing). Unrestricted principals keep 404-only
    # for truly missing objects.
    allowed = ensure_ip_access_or_all(session, principal)
    stmt = select(Operation).where(Operation.id == op_id)
    if allowed is not None:
        stmt = stmt.where(Operation.ip_id.in_(allowed))
    op = session.scalar(stmt)
    if op is None:
        raise HTTPException(status_code=404, detail="operation not found")
    # OWASP API1 — object-level authorization: the operation's ИП must be one
    # of the principal's allowed ИП, not just any row a role can name.
    ensure_ip_access(session, principal, op.ip_id)
    # TD-005: direction is not denormalised on operations — it lives on the type
    ot_direction = op.operation_type.direction if op.operation_type is not None else None
    return {
        "id": op.id,
        "ip_id": op.ip_id,
        "direction": ot_direction,
        "amount": str(op.amount),
        "operation_date": op.operation_date.isoformat(),
        "comment": op.comment,
        "import_fingerprint": op.import_fingerprint,
    }
