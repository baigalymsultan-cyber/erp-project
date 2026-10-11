"""Financial + analytical report routes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import _decimal_safe, get_db
from app.core.policy import assert_ip_exists, ensure_ip_access, ensure_ip_access_or_all
from app.core.security import require_privilege
from app.services import account_balances, counterparty_balances, pnl

router = APIRouter()


@router.get("/reports/cash-flow", tags=["finance"])
def report_cash_flow(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    ip_id: int = Query(),
    date_from: date = Query(),
    date_to: date = Query(),
) -> dict[str, object]:
    """§13 FINANCE-OPS-001 — «КУДА ДЕЛИСЬ МОИ ДЕНЬГИ?».

    Cash-flow breakdown with the fully specified §13 formula:
    START BALANCE + SALES + OTHER INFLOWS + PAYOUTS
    - MATERIALS - PAYROLL - SUPPLIERS - COMMISSIONS - REFUNDS
    - OPERATING EXPENSES - TRANSFERS OUT = ENDING BALANCE.
    Rule: transfer ≠ expense.
    """
    ensure_ip_access(session, principal, ip_id)
    assert_ip_exists(session, ip_id)
    if date_to < date_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_to must be >= date_from")
    # Cap the range (P2-3 pattern): max 366 days
    if (date_to - date_from).days > 366:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date range max 366 days")
    row = (
        session.execute(
            text("SELECT * FROM cash_flow(:ip, :df, :dt)"),
            {"ip": ip_id, "df": date_from, "dt": date_to},
        )
        .mappings()
        .one()
    )
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()}


@router.get("/reports/ip-summary", tags=["finance"])
def report_ip_summary(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    month_from: date | None = Query(default=None),
    ip_id: int | None = Query(default=None),
    month_to: date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E5 — monthly financial summary per ИП (v_ip_financial_summary)."""
    allowed = ensure_ip_access_or_all(session, principal)
    where = ["TRUE"]
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
    ips: list[int] | None
    if allowed is not None:
        ips = [ip_id] if ip_id is not None else list(allowed)
    else:
        ips = [ip_id] if ip_id is not None else None
    if ips is not None:
        where.append("ip_id = ANY(:ips)")
        params["ips"] = ips
    if month_from is not None:
        where.append("period_month >= :mf")
        params["mf"] = month_from
    if month_to is not None:
        where.append("period_month < (:mt::date + INTERVAL '1 month')")
        params["mt"] = month_to
    base = " FROM v_ip_financial_summary WHERE " + " AND ".join(where)
    total = session.execute(text("SELECT count(*)" + base), params).scalar_one()
    rows = session.execute(
        text("SELECT *" + base + " ORDER BY ip_id, period_month LIMIT :limit OFFSET :offset"),
        params,
    ).mappings()
    return {
        "total": int(total),
        "items": [_decimal_safe(dict(r)) for r in rows],
    }


@router.get("/pnl", tags=["finance"])
def get_pnl(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    period_start: date | None = None,
    period_end: date | None = None,
    ip_id: int | None = Query(default=None, gt=0),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    allowed = ensure_ip_access_or_all(session, principal)
    ip_scope: list[int] | None = allowed
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        ip_scope = [ip_id]
    period = None
    if period_start is not None and period_end is not None:
        from app.schemas import PayrollPeriodCreate as _P

        period = _P(period_start=period_start, period_end=period_end)
    result = pnl(session, period, ip_scope)
    return {key: str(value) for key, value in result.items()}


@router.get("/reports/counterparty-balances", tags=["finance"])
def get_counterparty_balances(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    ip_id: int | None = Query(default=None, gt=0),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """AR/AP Tier 1: net position per counterparty (GAP-003).

    Returns list of {ip_id, counterparty_id, counterparty_name,
    total_accrued, total_settled, balance}.
    Positive balance = AR (they owe us), negative = AP (we owe them).
    """
    allowed = ensure_ip_access_or_all(session, principal)
    ip_scope: list[int] | None = allowed
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        ip_scope = [ip_id]
    balances = counterparty_balances(session, ip_scope)
    # Convert Decimals to strings for JSON
    for b in balances:
        for key in ("total_accrued", "total_settled", "balance"):
            if b[key] is not None:
                b[key] = str(b[key])
    return {"items": balances, "count": len(balances)}


@router.get("/reports/account-balances", tags=["finance"])
def get_account_balances(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    ip_id: int | None = Query(default=None, gt=0),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Treasury: cash balance per account (GAP-002).

    Returns list of {ip_id, account_id, type, name, balance}.
    Answers "where is my money RIGHT NOW by account?"
    """
    allowed = ensure_ip_access_or_all(session, principal)
    ip_scope: list[int] | None = allowed
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        ip_scope = [ip_id]
    balances = account_balances(session, ip_scope)
    for b in balances:
        if b["balance"] is not None:
            b["balance"] = str(b["balance"])
    return {"items": balances, "count": len(balances)}


@router.get("/reports/wip", tags=["reports"])
def get_wip_report(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    ip_id: int = Query(gt=0),
) -> dict[str, object]:
    """P2-INV-004: derived WIP per open order (frozen cost + confirmed labour)."""
    ensure_ip_access(session, principal, ip_id)
    rows = session.execute(
        text(
            "SELECT order_id, external_order_id, status, wip_materials, "
            "wip_labour, wip_total FROM v_wip_by_order "
            "WHERE ip_id = :ip ORDER BY order_id"
        ),
        {"ip": ip_id},
    ).all()
    return {
        "ip_id": ip_id,
        "items": [
            {
                "order_id": r.order_id,
                "external_order_id": r.external_order_id,
                "status": r.status,
                "wip_materials": str(r.wip_materials),
                "wip_labour": str(r.wip_labour),
                "wip_total": str(r.wip_total),
            }
            for r in rows
        ],
    }
