"""Payroll + HR routes — §32/§33 rate freeze, §34–36 manual input."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import _decimal_safe, ensure_employee_access, get_db
from app.core.policy import assert_ip_exists, ensure_ip_access, ensure_ip_access_or_all
from app.core.security import require_privilege
from app.models.hr import Employee, PayrollPeriod, PayrollSummary, WorkEntry
from app.models.orders import Order
from app.models.reference import IpEntity
from app.schemas.payroll import (
    PayrollCalculateRequest,
    PayrollPayRequest,
    PayrollPeriodCreate,
    WorkEntryCreate,
)
from app.services import (
    add_work_entry,
    approve_payroll_summary,
    calculate_payroll,
    pay_payroll_summary,
    unapprove_payroll_summary,
)

router = APIRouter()


@router.get("/reports/payroll-due", tags=["payroll"])
def report_payroll_due(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    ip_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E4 — v_payroll_due (§63): outstanding salary per employee.

    ИП-isolated server-side (REQ-TD016-02); replaces UI read-SQL B3/A19.
    """
    allowed = ensure_ip_access_or_all(session, principal)
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        allowed = [ip_id]

    where = "WHERE ps.summary_status <> 'CANCELLED'"
    params: dict[str, object] = {}
    if allowed is not None:
        where += " AND e.assigned_ip_id = ANY(:allowed)"
        params["allowed"] = allowed
    base = f" FROM v_payroll_due ps JOIN employees e ON e.id = ps.employee_id {where} "
    total = session.execute(text("SELECT count(*)" + base), params).scalar_one()
    rows = session.execute(
        text(
            "SELECT ps.payroll_summary_id, ps.employee_id, ps.employee_name,"
            " ps.payment_type, ps.period_start, ps.period_end, ps.period_status,"
            " e.assigned_ip_id AS ip_id, ps.piecework_total, ps.base_salary, ps.bonuses,"
            " ps.deductions, ps.total_calculated, ps.amount_paid, ps.amount_due,"
            " ps.summary_status, ps.payments_count"
            + base
            + " ORDER BY ps.payroll_summary_id DESC LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": limit, "offset": offset},
    ).all()
    items = [
        {
            "payroll_summary_id": r.payroll_summary_id,
            "employee_id": r.employee_id,
            "employee_name": r.employee_name,
            "payment_type": r.payment_type,
            "period_start": r.period_start.isoformat(),
            "period_end": r.period_end.isoformat(),
            "period_status": r.period_status,
            "ip_id": r.ip_id,
            "piecework_total": str(r.piecework_total),
            "base_salary": str(r.base_salary),
            "bonuses": str(r.bonuses),
            "deductions": str(r.deductions),
            "total_calculated": str(r.total_calculated),
            "amount_paid": str(r.amount_paid),
            "amount_due": str(r.amount_due),
            "summary_status": r.summary_status,
            "payments_count": r.payments_count,
        }
        for r in rows
    ]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/reports/work-entries/summary", tags=["payroll"])
def work_entries_summary(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
) -> dict[str, object]:
    """TD-016 E21 — KPI for UI 'Производство' (replaces A17 KPI SQL)."""
    allowed = ensure_ip_access_or_all(session, principal)
    where = [
        "we.ip_id = e.assigned_ip_id",
        "(we.order_id IS NULL OR o.ip_id = e.assigned_ip_id)",
    ]
    params: dict[str, object] = {}
    if date_from is not None:
        where.append("we.entry_date >= :df")
        params["df"] = date_from
    if date_to is not None:
        where.append("we.entry_date <= :dt")
        params["dt"] = date_to
    if allowed is not None:
        where.append("e.assigned_ip_id = ANY(:allowed)")
        params["allowed"] = allowed
    row = session.execute(
        text(
            "SELECT count(*) AS n, COALESCE(SUM(we.total_sum), 0) AS total"
            " FROM work_entries we"
            " JOIN employees e ON e.id = we.employee_id"
            " LEFT JOIN orders o ON o.id = we.order_id"
            f" WHERE {' AND '.join(where)}"
        ),
        params,
    ).one()
    return {"count": int(row.n), "total_sum": str(row.total)}


@router.get("/reports/payroll-due/summary", tags=["payroll"])
def payroll_due_summary(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """TD-016 E23 — KPI for UI 'Зарплата' (replaces A19 KPI SQL)."""
    allowed = ensure_ip_access_or_all(session, principal)
    where = "WHERE ps.summary_status <> 'CANCELLED'"
    params: dict[str, object] = {}
    if allowed is not None:
        where += " AND e.assigned_ip_id = ANY(:allowed)"
        params["allowed"] = allowed
    row = session.execute(
        text(
            "SELECT COALESCE(SUM(total_calculated), 0) AS calc,"
            " COALESCE(SUM(amount_paid), 0) AS paid,"
            " COALESCE(SUM(amount_due), 0) AS due"
            f" FROM v_payroll_due ps JOIN employees e ON e.id = ps.employee_id {where}"
        ),
        params,
    ).one()
    return {"calculated": str(row.calc), "paid": str(row.paid), "due": str(row.due)}


@router.get("/reports/work-entries", tags=["payroll"])
def report_work_entries(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    ip_id: int | None = Query(default=None),
    employee_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status", max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E8 — piecework entries with frozen rates (§33, v_work_entries_detail)."""
    allowed = ensure_ip_access_or_all(session, principal)
    where = [
        "EXISTS (SELECT 1 FROM employees e WHERE e.id = v.employee_id "
        "AND e.assigned_ip_id = v.ip_id)",
        "(v.order_id IS NULL OR EXISTS (SELECT 1 FROM orders o "
        "WHERE o.id = v.order_id AND o.ip_id = v.ip_id))",
    ]
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
    ips: list[int] | None
    if allowed is not None:
        ips = [ip_id] if ip_id is not None else list(allowed)
    else:
        ips = [ip_id] if ip_id is not None else None
    if ips is not None:
        where.append("v.ip_id = ANY(:ips)")
        params["ips"] = ips
    if date_from is not None:
        where.append("v.entry_date >= :df")
        params["df"] = date_from
    if date_to is not None:
        where.append("v.entry_date <= :dt")
        params["dt"] = date_to
    if employee_id is not None:
        ensure_employee_access(session, principal, employee_id)
        where.append("v.employee_id = :emp")
        params["emp"] = employee_id
    if status_filter is not None:
        where.append("v.status = :st")
        params["st"] = status_filter
    base = " FROM v_work_entries_detail v WHERE " + " AND ".join(where)
    total = session.execute(text("SELECT count(*)" + base), params).scalar_one()
    rows = session.execute(
        text("SELECT *" + base + " ORDER BY work_entry_id LIMIT :limit OFFSET :offset"),
        params,
    ).mappings()
    return {
        "total": int(total),
        "items": [_decimal_safe(dict(r)) for r in rows],
    }


@router.post("/payroll/periods", status_code=status.HTTP_201_CREATED, tags=["payroll"])
def post_payroll_period(
    data: PayrollPeriodCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("DIRECTOR"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    # SEC-003: payroll periods are company-global and the overlap EXCLUDE
    # constraint makes one bad write a payroll-wide DoS. Creation requires
    # DIRECTOR+ (ADMIN passes); IP-restricted ACCOUNTANTs must not create
    # global periods.
    # NOTE: PayrollPeriodCreate.comment is accepted but has no column to land
    # in (payroll_periods has no comment column) — intentionally not passed.
    period = PayrollPeriod(
        start_date=data.period_start,
        end_date=data.period_end,
    )
    session.add(period)
    session.flush()
    return {"id": period.id, "status": period.status}


@router.post(
    "/payroll/work-entries",
    status_code=status.HTTP_201_CREATED,
    tags=["payroll"],
)
def post_work_entry(
    data: WorkEntryCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("BRIGADIER"))],
    session: Session = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=64)] = None,
) -> dict[str, object]:
    """§61/§32 — the rate is resolved automatically at save time."""
    ensure_employee_access(session, principal, data.employee_id)
    employee = session.get(Employee, data.employee_id)
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "employee not found")
    if employee.assigned_ip_id is not None:
        ensure_ip_access(session, principal, employee.assigned_ip_id)
    ensure_ip_access(session, principal, data.ip_id)
    assert_ip_exists(session, data.ip_id)
    if data.order_id is not None:
        order = session.get(Order, data.order_id)
        if order is None:
            ensure_ip_access(session, principal, None)
            raise HTTPException(status.HTTP_404_NOT_FOUND, "order not found")
        ensure_ip_access(session, principal, order.ip_id)
    # C-2: optional Idempotency-Key header for safe retries. Maps to
    # import_fingerprint (uq_work_entries_import_fingerprint). Check first:
    # if the key was already used, return the existing entry.
    fingerprint = data.import_fingerprint
    if idempotency_key and not fingerprint:
        fingerprint = idempotency_key.strip()
    if fingerprint:
        # P1-1/M-1: scope the replay lookup to the request's IP (cross-IP leak fix).
        existing = session.scalar(
            select(WorkEntry).where(
                WorkEntry.import_fingerprint == fingerprint,
                WorkEntry.ip_id == data.ip_id,
            )
        )
        if existing is not None:
            return {
                "id": existing.id,
                "status": "IDEMPOTENT_REPLAY",
                "rate_at_time": str(existing.rate_at_time),
                "total_sum": str(existing.total_sum),
            }
        data = data.model_copy(update={"import_fingerprint": fingerprint})
    # P2-2: race-safe insert (SAVEPOINT; same pattern as post_operation).
    entry_id: int | None = None
    try:
        with session.begin_nested():
            entry_id = add_work_entry(session, data)
    except IntegrityError as exc:
        if not (fingerprint and "uq_work_entries_import_fingerprint" in str(exc.orig or exc)):
            raise
        existing = session.scalar(
            select(WorkEntry).where(
                WorkEntry.import_fingerprint == fingerprint,
                WorkEntry.ip_id == data.ip_id,
            )
        )
        if existing is not None:
            return {
                "id": existing.id,
                "status": "IDEMPOTENT_REPLAY",
                "rate_at_time": str(existing.rate_at_time),
                "total_sum": str(existing.total_sum),
            }
        raise
    assert entry_id is not None
    entry = session.get(WorkEntry, entry_id)
    assert entry is not None  # flushed in the same transaction
    return {
        "id": entry_id,
        "status": entry.status,
        "rate_at_time": str(entry.rate_at_time),
        "total_sum": str(entry.total_sum),
    }


@router.get("/payroll/summary", tags=["payroll"])
def payroll_summary(
    employee_id: int,
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    ensure_employee_access(session, principal, employee_id)
    rows = session.query(PayrollSummary).filter(PayrollSummary.employee_id == employee_id).all()
    return {
        "employee_id": employee_id,
        "periods": [
            {
                "id": r.id,
                "piecework_total": str(r.piecework_total),
                "base_salary": str(r.base_salary),
                "total_calculated": str(r.total_calculated),
                "status": r.status,
            }
            for r in rows
        ],
    }


@router.post("/payroll/calculate", tags=["payroll"])
def post_payroll_calculate(
    data: PayrollCalculateRequest,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """§35 — build/refresh payroll summaries for a period (idempotent)."""
    ip_ids = ensure_ip_access_or_all(session, principal)
    for adjustment in data.adjustments:
        ensure_employee_access(session, principal, adjustment.employee_id)
    adjustments = {adj.employee_id: (adj.bonuses, adj.deductions) for adj in data.adjustments}
    summaries = calculate_payroll(session, data.period_id, adjustments, ip_ids=ip_ids)
    return {"period_id": data.period_id, "summaries": summaries}


@router.post("/payroll/summaries/{summary_id}/approve", tags=["payroll"])
def post_payroll_approve(
    summary_id: int,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """CALCULATED → APPROVED; only APPROVED summaries may be paid (§36)."""
    summary = session.get(PayrollSummary, summary_id)
    if summary is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payroll summary not found")
    # SEC-002(b): pin authorization to the summary's frozen ip_id (set at
    # calculate time), not the employee's live assignment — reassignment
    # must not transfer approval power over another IP's payroll.
    # ensure_ip_access is fail-closed: NULL ip_id + restricted principal = 403.
    ensure_ip_access(session, principal, summary.ip_id)
    return approve_payroll_summary(session, summary_id)


@router.post("/payroll/summaries/{summary_id}/unapprove", tags=["payroll"])
def post_payroll_unapprove(
    summary_id: int,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """APPROVED → CALCULATED; reverses approval so summary can be recalculated (PAY-001)."""
    summary = session.get(PayrollSummary, summary_id)
    if summary is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payroll summary not found")
    # SEC-002(b): pin authorization to the summary's frozen ip_id (set at
    # calculate time), not the employee's live assignment — reassignment
    # must not transfer approval power over another IP's payroll.
    # ensure_ip_access is fail-closed: NULL ip_id + restricted principal = 403.
    ensure_ip_access(session, principal, summary.ip_id)
    try:
        return unapprove_payroll_summary(session, summary_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/payroll/pay", tags=["payroll"])
def post_payroll_pay(
    data: PayrollPayRequest,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """§36/§57 — pay an APPROVED summary exactly once, atomically."""
    summary = session.get(PayrollSummary, data.summary_id)
    if summary is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payroll summary not found")
    # SEC-002(b): pin authorization to the summary's frozen ip_id (set at
    # calculate time), not the employee's live assignment — reassignment
    # must not transfer approval power over another IP's payroll.
    # ensure_ip_access is fail-closed: NULL ip_id + restricted principal = 403.
    ensure_ip_access(session, principal, summary.ip_id)
    user_id = principal.get("user_id")
    return pay_payroll_summary(
        session,
        data.summary_id,
        data.payment_date,
        data.payment_method,
        data.comment,
        created_by=int(user_id) if isinstance(user_id, int) else None,
    )


@router.get("/employees", tags=["hr"])
def list_employees(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    status_filter: Literal["ACTIVE", "ON_LEAVE", "FIRED"] | None = Query(
        default=None, alias="status"
    ),
    ip_id: int | None = Query(default=None),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E14 — employees for the UI HR/reference pickers.

    Property-level authorisation: sensitive compensation fields
    (phone, base_salary) are returned only to HR/ADMIN/DIRECTOR; other
    roles get name/position/status for employees assigned to ИП in
    their allow-list (OWASP API1/API6).
    TD-016 P5: ip_id/q/ON_LEAVE filters, hired/fired dates and the ИП
    name back the admin 'Сотрудники' module (replaces its raw SQL).
    """
    roles = {str(r) for r in cast(list[str], principal["roles"])}
    privileged = bool(roles & {"HR", "ADMIN", "DIRECTOR"})
    allowed = ensure_ip_access_or_all(session, principal)
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        if allowed is not None:
            allowed = [ip_id]
    stmt = select(Employee)
    count_stmt = select(func.count()).select_from(Employee)
    active_stmt = select(func.count()).select_from(Employee)
    like = f"%{(q or '').strip()}%"
    if not privileged:
        if allowed is None:
            allowed = [
                row[0]
                for row in session.execute(
                    select(Employee.assigned_ip_id).where(Employee.assigned_ip_id.is_not(None))
                )
            ]
        restricted = allowed or (-1,)
        stmt = stmt.where(Employee.assigned_ip_id.in_(restricted))
        count_stmt = count_stmt.where(Employee.assigned_ip_id.in_(restricted))
        active_stmt = active_stmt.where(Employee.assigned_ip_id.in_(restricted))
    if status_filter is not None:
        stmt = stmt.where(Employee.status == status_filter)
        count_stmt = count_stmt.where(Employee.status == status_filter)
        active_stmt = active_stmt.where(Employee.status == status_filter)
    if like != "%%":
        cond = or_(Employee.full_name.ilike(like), Employee.position.ilike(like))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
        active_stmt = active_stmt.where(cond)
    total = session.execute(count_stmt).scalar_one()
    active = session.execute(active_stmt.where(Employee.status == "ACTIVE")).scalar_one()
    name_by_ip: dict[int, str] = {
        int(r[0]): str(r[1]) for r in session.execute(select(IpEntity.id, IpEntity.name))
    }
    rows = session.execute(
        stmt.order_by(Employee.status, Employee.full_name).limit(limit).offset(offset)
    ).scalars()
    items: list[dict[str, object]] = []
    for r in rows:
        item: dict[str, object] = {
            "id": r.id,
            "full_name": r.full_name,
            "position": r.position,
            "payment_type": r.payment_type,
            "status": r.status,
            "assigned_ip_id": r.assigned_ip_id,
            "assigned_ip_name": (
                name_by_ip.get(r.assigned_ip_id) if r.assigned_ip_id is not None else None
            ),
        }
        if privileged:
            item["phone"] = r.phone
            item["base_salary"] = str(r.base_salary) if r.base_salary else None
            item["hired_at"] = r.hired_at.isoformat() if r.hired_at else None
            item["fired_at"] = r.fired_at.isoformat() if r.fired_at else None
        items.append(item)
    return {
        "total": int(total),
        "active": int(active),
        "items": items,
    }
