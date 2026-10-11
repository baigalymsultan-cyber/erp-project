"""Payroll domain services — calculation, approval, payment (§32–§36)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.finance import Operation
from app.models.hr import RateHistory, WorkEntry
from app.models.orders import Order
from app.models.reference import OperationType
from app.schemas import PayrollPeriodCreate, WorkEntryCreate
from app.services.common import _q2


class PayrollSummaryLike(Protocol):
    """Structural view of PayrollSummary rows (§35) — avoids a circular import."""

    id: int
    employee_id: int
    payroll_period_id: int
    piecework_total: Decimal
    base_salary: Decimal
    bonuses: Decimal
    deductions: Decimal
    status: str



def effective_rate(session: Session, work_operation_type_id: int, on: date) -> Decimal | None:
    """Rate in force on `on` for the given work operation type.

    Periods of one type never overlap (EXCLUDE constraint excl_rates_history_
    overlap, §31), so at most one row can match. The open interval
    (effective_to IS NULL) is the currently active rate.
    """
    stmt = (
        select(RateHistory.rate)
        .where(RateHistory.work_operation_type_id == work_operation_type_id)
        .where(RateHistory.effective_from <= on)
        .where((RateHistory.effective_to.is_(None)) | (RateHistory.effective_to > on))
        .order_by(RateHistory.effective_from.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def add_work_entry(session: Session, data: WorkEntryCreate) -> int:
    """Insert a work entry with the rate FROZEN at entry time (§32).

    The rate is taken from rates_history for entry_date; the stored
    total_sum is quantity * rate computed once and never recalculated
    (§33: changing the rate later must not alter historical entries —
    enforced by the fn_work_entries_immutable trigger).

    §61 — data.order_id optionally binds the entry to an order; the
    order's direct cost then includes this labour amount (§37).
    """
    from app.models.hr import Employee

    employee = (
        session.execute(select(Employee).where(Employee.id == data.employee_id).with_for_update())
        .scalars()
        .first()
    )
    if employee is None:
        raise ValueError(f"employee {data.employee_id} does not exist")
    if employee.assigned_ip_id is None:
        raise ValueError(f"employee {data.employee_id} has no assigned ИП")
    if employee.assigned_ip_id != data.ip_id:
        raise ValueError("work entry ИП must match the employee's assigned ИП")
    if data.order_id is not None:
        order = (
            session.execute(select(Order).where(Order.id == data.order_id).with_for_update())
            .scalars()
            .first()
        )
        if order is None:
            raise ValueError(f"order {data.order_id} does not exist")
        if order.ip_id != employee.assigned_ip_id:
            raise ValueError("work entry order ИП must match the employee's assigned ИП")

    rate = effective_rate(session, data.work_operation_type_id, data.entry_date)
    if rate is None:
        raise ValueError(
            f"no rate in force for work_operation_type "
            f"{data.work_operation_type_id} on {data.entry_date}"
        )
    entry = WorkEntry(
        employee_id=data.employee_id,
        work_operation_type_id=data.work_operation_type_id,
        order_id=data.order_id,
        ip_id=data.ip_id,
        entry_date=data.entry_date,
        quantity=data.quantity,
        rate_at_time=rate,
        comment=data.comment,
        # C-2: idempotency via import_fingerprint (uq_work_entries_import_fingerprint)
        import_fingerprint=data.import_fingerprint,
    )
    session.add(entry)
    session.flush()
    return entry.id


def payroll_period_totals(
    session: Session,
    period: PayrollPeriodCreate,
    ip_ids: list[int] | None = None,
) -> dict[str, Decimal]:
    """Accruals for a payroll period (§34–36).

    The composition of the period (base salary, bonuses, deductions) is
    entered manually; the service does not impose a formula (OQ-007).

    When ``ip_ids`` is supplied, only work entries of those ИПs are
    counted; without it the totals span all ИПs (cross-IP callers must
    pass their allow-list explicitly — see ``calculate_payroll``).
    """
    stmt = (
        select(func.coalesce(func.sum(WorkEntry.total_sum), 0))
        .where(WorkEntry.ip_id.isnot(None))
        .where(WorkEntry.entry_date >= period.period_start)
        .where(WorkEntry.entry_date <= period.period_end)
    )
    if ip_ids is not None:
        stmt = stmt.where(WorkEntry.ip_id.in_(ip_ids))
    accrued = _q2(Decimal(session.scalar(stmt) or 0))
    return {"accrued": accrued, "to_pay": accrued}


def _lookup_operation_type_id(session: Session, code: str) -> int:
    type_id = session.scalar(select(OperationType.id).where(OperationType.code == code))
    if type_id is None:
        raise ValueError(f"operation type {code!r} is not seeded (§20)")
    return int(type_id)


def calculate_payroll(
    session: Session,
    period_id: int,
    adjustments: dict[int, tuple[Decimal, Decimal]] | None = None,
    ip_ids: list[int] | None = None,
) -> list[dict[str, object]]:
    """Build payroll summaries for a period (§35).

    piecework_total = sum(total_sum) of CONFIRMED work entries dated in the
    period; base_salary = employees.base_salary for SALARY / MIXED (§35);
    bonuses / deductions are manual (OQ-007 — composition is entered, not
    derived). When ``ip_ids`` is supplied, both employee summaries and their
    work-entry inputs are limited to those assigned ИПs; inconsistent
    employee/IP/order links are excluded.

    Idempotent: re-running recalculates the same rows in place
    (uq_payroll_summary_employee_period backstops races, §57). Rows already
    PAID are never recalculated. The period moves to PROCESSING; it must not
    be CLOSED/PAID yet.
    """
    from app.models.hr import Employee, PayrollPeriod, PayrollSummary, WorkEntry

    period = session.get(PayrollPeriod, period_id)
    if period is None:
        raise ValueError(f"payroll period {period_id} does not exist")
    if period.status in ("CLOSED", "PAID"):
        raise ValueError(
            f"§34: period {period_id} is {period.status}; calculation needs an OPEN period"
        )
    start: date = period.start_date
    end: date = period.end_date
    if end < start:
        raise ValueError("§34: end_date must be >= start_date")
    adjustments = adjustments or {}

    # §57 — lock the period row so two concurrent calculations serialize.
    session.execute(select(PayrollPeriod.id).where(PayrollPeriod.id == period_id).with_for_update())

    piecework_stmt = (
        select(
            WorkEntry.employee_id,
            func.coalesce(func.sum(WorkEntry.total_sum), 0).label("piecework"),
        )
        .join(Employee, Employee.id == WorkEntry.employee_id)
        .outerjoin(Order, Order.id == WorkEntry.order_id)
        .where(WorkEntry.entry_date >= start)
        .where(WorkEntry.entry_date <= end)
        .where(WorkEntry.status == "CONFIRMED")
        .where(WorkEntry.ip_id == Employee.assigned_ip_id)
        .where(or_(WorkEntry.order_id.is_(None), Order.ip_id == Employee.assigned_ip_id))
        .group_by(WorkEntry.employee_id)
    )
    employee_stmt = select(Employee)
    if ip_ids is not None:
        piecework_stmt = piecework_stmt.where(Employee.assigned_ip_id.in_(ip_ids))
        employee_stmt = employee_stmt.where(Employee.assigned_ip_id.in_(ip_ids))
    pieces = session.execute(piecework_stmt).all()
    piecework_by_employee = {int(row.employee_id): _q2(Decimal(row.piecework)) for row in pieces}

    employee_rows = session.execute(employee_stmt).scalars().all()
    result: list[dict[str, object]] = []
    for employee in employee_rows:
        piecework_total = piecework_by_employee.get(employee.id, Decimal("0"))
        base_salary = (
            _q2(Decimal(employee.base_salary))
            if employee.payment_type in ("SALARY", "MIXED") and employee.base_salary is not None
            else Decimal("0")
        )
        bonuses, deductions = adjustments.get(employee.id, (Decimal("0"), Decimal("0")))

        summary = (
            session.execute(
                select(PayrollSummary)
                .where(PayrollSummary.employee_id == employee.id)
                .where(PayrollSummary.payroll_period_id == period_id)
                .with_for_update()
            )
            .scalars()
            .first()
        )
        if (
            summary is None
            and piecework_total == 0
            and base_salary == 0
            and bonuses == 0
            and deductions == 0
        ):
            # Nothing to pay — a zero summary is ledger noise, not payroll.
            continue
        if summary is not None and summary.status == "PAID":
            # §57 — a paid summary is immutable history; skip it.
            result.append(_summary_payload(summary))
            continue
        if summary is not None and summary.status == "APPROVED":
            # P2-3 — an approval is a control gate (§36/§81). Silently demoting
            # APPROVED → CALCULATED would void the approver's sign-off without
            # an explicit un-approve step. Refuse; the caller must un-approve
            # first (explicit action, audited).
            raise ValueError(
                f"§36: summary {summary.id} is APPROVED; un-approve it before recalculating"
            )
        if summary is None:
            summary = PayrollSummary(
                employee_id=employee.id,
                payroll_period_id=period_id,
                ip_id=employee.assigned_ip_id,
                piecework_total=piecework_total,
                base_salary=base_salary,
                bonuses=bonuses,
                deductions=deductions,
                status="CALCULATED",
                calculated_at=datetime.now(UTC),
            )
            session.add(summary)
        else:
            # SEC-002(b): ip_id is frozen at first calculation — recalculation
            # must not move the summary to the employee's new IP (that would
            # transfer approval power over another IP's payroll). Only repair
            # NULL (legacy rows predating the binding).
            if summary.ip_id is None:
                summary.ip_id = employee.assigned_ip_id
            summary.piecework_total = piecework_total
            summary.base_salary = base_salary
            summary.bonuses = bonuses
            summary.deductions = deductions
            summary.status = "CALCULATED"
            summary.calculated_at = datetime.now(UTC)
        session.flush()
        result.append(_summary_payload(summary))

    if period.status == "OPEN":
        period.status = "PROCESSING"
    session.flush()
    return result


def _summary_payload(summary: PayrollSummaryLike) -> dict[str, object]:
    return {
        "id": summary.id,
        "employee_id": summary.employee_id,
        "payroll_period_id": summary.payroll_period_id,
        "piecework_total": str(summary.piecework_total),
        "base_salary": str(summary.base_salary),
        "bonuses": str(summary.bonuses),
        "deductions": str(summary.deductions),
        "total_calculated": str(summary.total_calculated),  # type: ignore[attr-defined]
        "status": summary.status,
    }


def approve_payroll_summary(session: Session, summary_id: int) -> dict[str, object]:
    """CALCULATED → APPROVED. Only APPROVED summaries may be paid (§36/§81)."""
    from app.models.hr import PayrollSummary

    summary = session.get(PayrollSummary, summary_id)
    if summary is None:
        raise ValueError(f"payroll summary {summary_id} does not exist")
    if summary.status == "PAID":
        raise ValueError(f"§36: summary {summary_id} is already PAID")
    if summary.status != "CALCULATED":
        raise ValueError(
            f"§36: summary {summary_id} is {summary.status}; only CALCULATED can be approved"
        )
    summary.status = "APPROVED"
    session.flush()
    return _summary_payload(summary)


def unapprove_payroll_summary(session: Session, summary_id: int) -> dict[str, object]:
    """APPROVED → CALCULATED. Reverses approval so summary can be recalculated.

    PAY-001: calculate_payroll refuses APPROVED summaries with message
    "un-approve it before recalculating" but no un-approve existed —
    dead-end workflow. This closes it.

    Guards:
    - PAID summaries cannot be un-approved (money already moved)
    - Only APPROVED → CALCULATED transition allowed
    """
    from app.models.hr import PayrollSummary

    summary = session.get(PayrollSummary, summary_id)
    if summary is None:
        raise ValueError(f"payroll summary {summary_id} does not exist")
    if summary.status == "PAID":
        raise ValueError(f"§36: summary {summary_id} is PAID; cannot un-approve a paid summary")
    if summary.status != "APPROVED":
        raise ValueError(
            f"§36: summary {summary_id} is {summary.status}; only APPROVED can be un-approved"
        )
    summary.status = "CALCULATED"
    session.flush()
    return _summary_payload(summary)


def pay_payroll_summary(
    session: Session,
    summary_id: int,
    payment_date: date,
    payment_method: str,
    comment: str | None = None,
    created_by: int | None = None,
) -> dict[str, object]:
    """Pay an APPROVED summary (§36) with the §57 double-payment guard.

    Flow inside ONE transaction:
      1. SELECT ... FOR UPDATE the payroll summary row — serializes racers;
      2. status must be APPROVED (CALCULATED/CANCELLED/PAID are refused);
      3. append a SALARY_PAYMENT expense operation to the ledger (§16);
      4. insert payroll_payments with operation_id — the partial unique index
         uq_payroll_payments_active makes a second POSTED payment impossible
         even in a race;
      5. mark the summary PAID and freeze its CONFIRMED work entries as PAID
         with the period reference (chk_work_entries_paid_period).
    Any failure rolls the whole thing back — money never moves twice.
    """
    from app.models.hr import PayrollPayment, PayrollPeriod, PayrollSummary, WorkEntry

    # 1. §57 — lock the summary row for the rest of the transaction.
    summary = (
        session.execute(
            select(PayrollSummary).where(PayrollSummary.id == summary_id).with_for_update()
        )
        .scalars()
        .first()
    )
    if summary is None:
        raise ValueError(f"payroll summary {summary_id} does not exist")
    # 2.
    if summary.status == "PAID":
        raise ValueError(f"§57: summary {summary_id} is already PAID — double payment blocked")
    if summary.status != "APPROVED":
        raise ValueError(
            f"§36: summary {summary_id} is {summary.status}; only APPROVED summaries can be paid"
        )

    amount = _q2(Decimal(summary.total_calculated))
    if amount <= 0:
        raise ValueError(f"§36: summary {summary_id} totals {amount}; nothing to pay")

    period = session.get(PayrollPeriod, summary.payroll_period_id)
    assert period is not None  # FK guarantees existence

    # 3. Ledger: SALARY_PAYMENT is OUT/EXPENSE — §27 stores OUT amounts negative
    #    (fn_check_operation_sign); payroll_payments.amount stays positive.
    op_type_id = _lookup_operation_type_id(session, "SALARY_PAYMENT")
    if summary.employee is None or summary.employee.assigned_ip_id is None:
        raise ValueError(
            f"employee {summary.employee_id} has no assigned ИП — cannot post salary expense"
        )
    employee_ip_id = summary.employee.assigned_ip_id
    operation = Operation(
        ip_id=employee_ip_id,
        type_id=op_type_id,
        amount=-amount,  # §27: OUT operations are stored negative
        operation_date=payment_date,
        # P&L: salary payment IS the economic event (calculate_payroll posts
        # zero operations). Set accrual_date so it hits P&L (PNL_AUDIT V3).
        accrual_date=payment_date,
        comment=comment or f"Выплата ЗП за период {period.start_date}..{period.end_date}",
    )
    session.add(operation)
    session.flush()

    # 4. Payment row; the partial unique index blocks a second POSTED payment.
    payment = PayrollPayment(
        payroll_summary_id=summary_id,
        payment_date=payment_date,
        amount=amount,
        payment_method=payment_method,
        operation_id=operation.id,
        status="POSTED",
        comment=comment,
        created_by=created_by,
    )
    session.add(payment)
    try:
        session.flush()
    except IntegrityError as exc:
        raise ValueError(
            f"§57: summary {summary_id} already has a POSTED payment — double payment blocked"
        ) from exc

    # 5. Close the loop.
    summary.status = "PAID"
    period.status = "PAID"
    period.closed_at = datetime.now(UTC)
    session.execute(
        update(WorkEntry)
        .where(
            WorkEntry.employee_id == summary.employee_id,
            WorkEntry.ip_id == employee_ip_id,
            WorkEntry.entry_date >= period.start_date,
            WorkEntry.entry_date <= period.end_date,
            WorkEntry.status == "CONFIRMED",
            or_(
                WorkEntry.order_id.is_(None),
                WorkEntry.order.has(Order.ip_id == employee_ip_id),
            ),
        )
        .values(status="PAID", payroll_period_id=period.id)
    )
    session.flush()
    return {
        "payment_id": payment.id,
        "operation_id": operation.id,
        "summary_id": summary_id,
        "amount": str(amount),
        "payment_method": payment_method,
        "summary_status": summary.status,
        "period_status": period.status,
    }
