"""Kaspi Shop/Pay routes — §24, §42."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import _decimal_safe, get_db
from app.core.policy import assert_ip_exists, ensure_ip_access, ensure_ip_access_or_all
from app.core.security import require_privilege
from app.models.kaspi import KaspiPayout, SyncLog
from app.schemas.kaspi import KaspiPayoutCreate
from app.services import reconcile_payout_full

router = APIRouter()


@router.get("/reports/kaspi-reconciliation", tags=["kaspi"])
def report_kaspi_reconciliation(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    match_status: Literal["MATCHED", "UNMATCHED", "EXTRA", "MISSING"] | None = Query(default=None),
    ip_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E6 — order↔payout reconciliation view, ИП-isolated."""
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
    if match_status is not None:
        where.append("match_status = :ms")
        params["ms"] = match_status
    base = " FROM v_kaspi_reconciliation WHERE " + " AND ".join(where)
    total = session.execute(text("SELECT count(*)" + base), params).scalar_one()
    rows = session.execute(
        text("SELECT *" + base + " ORDER BY order_id, payout_item_id LIMIT :limit OFFSET :offset"),
        params,
    ).mappings()
    return {
        "total": int(total),
        "items": [_decimal_safe(dict(r)) for r in rows],
    }


@router.get("/kaspi/sync-log", tags=["kaspi"])
def list_sync_log(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    ip_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E12 — Kaspi sync runs (UI 'Kaspi Синхронизация' table)."""
    allowed = ensure_ip_access_or_all(session, principal)
    stmt = select(SyncLog)
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        stmt = stmt.where(SyncLog.ip_id == ip_id)
    elif allowed is not None:
        stmt = stmt.where(SyncLog.ip_id.in_(allowed))
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(stmt.order_by(SyncLog.id.desc()).limit(limit).offset(offset)).scalars()
    return {
        "total": int(total),
        "items": [
            {
                "id": r.id,
                "ip_id": r.ip_id,
                "api_type": r.api_type,
                "status": r.status,
                "sync_started_at": r.sync_started_at.isoformat(),
                "sync_finished_at": (
                    r.sync_finished_at.isoformat() if r.sync_finished_at else None
                ),
                "records_fetched": r.records_fetched,
                "records_inserted": r.records_inserted,
                "records_updated": r.records_updated,
                "errors_count": r.errors_count,
            }
            for r in rows
        ],
    }


@router.get("/kaspi/payouts", tags=["kaspi"])
def list_kaspi_payouts(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    ip_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """§74 — paginated listing; the page size is capped server-side.

    BOLA (audit #1): the listing is always restricted to the ИП the
    principal may see, even when no ip_id filter was requested.
    """
    allowed = ensure_ip_access_or_all(session, principal)
    stmt = select(KaspiPayout)
    if allowed is not None:
        allowed_ids = set(allowed)
        if ip_id is not None and ip_id not in allowed_ids:
            return {"count": 0, "total": 0, "next_offset": None, "payouts": []}
        if allowed:
            stmt = stmt.where(KaspiPayout.ip_id.in_(allowed_ids))
        else:
            return {"count": 0, "total": 0, "next_offset": None, "payouts": []}
    elif ip_id is not None:
        stmt = stmt.where(KaspiPayout.ip_id == ip_id)
    limit = max(0, min(limit, 200))
    offset = max(0, offset)
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.scalars(stmt.order_by(KaspiPayout.id).limit(limit).offset(offset)).all()
    next_offset = offset + limit if offset + limit < (total or 0) else None
    return {
        "count": len(rows),
        "total": total,
        "next_offset": next_offset,
        "payouts": [
            {
                "id": r.id,
                "ip_id": r.ip_id,
                "payout_date": r.payout_date.isoformat(),
                "gross_amount": str(r.gross_amount),
                "commission_amount": str(r.commission_amount),
                "net_amount": str(r.net_amount),
                "status": r.status,
            }
            for r in rows
        ],
    }


@router.post("/kaspi/payouts", status_code=status.HTTP_201_CREATED, tags=["kaspi"])
def post_kaspi_payout(
    data: KaspiPayoutCreate,
    response: Response,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Register a payout envelope; amounts are stored as received (§94).

    F-ETL-010: idempotent on (ip_id, external_payout_id). A duplicate POST
    (retry-after-timeout) returns 200 with the existing row instead of a
    raw 500 — same SAVEPOINT pattern as post_operation (P2-1). The
    ``idempotent_replay`` flag marks the replay; ``status`` keeps the
    payout's domain status (EXPECTED/RECEIVED), not a write marker.
    """
    ensure_ip_access(session, principal, data.ip_id)
    assert_ip_exists(session, data.ip_id)
    # §24 — net_amount is NOT NULL in the schema; for an EXPECTED envelope it
    # is derived (gross − commission) until the real payout statement arrives.
    payout = KaspiPayout(
        ip_id=data.ip_id,
        external_payout_id=data.external_payout_id,
        payout_date=data.payout_date,
        gross_amount=data.gross_amount,
        commission_amount=data.commission_amount,
        net_amount=data.gross_amount - data.commission_amount,
        status=data.status,
    )
    try:
        with session.begin_nested():
            session.add(payout)
            session.flush()
    except IntegrityError as exc:
        # Concurrent/duplicate POSTs can both pass validation; the loser
        # hits uq_kaspi_payouts_external. Return the winner's row (200),
        # never a 500. Any other IntegrityError is a real bug -> re-raise.
        if "uq_kaspi_payouts_external" not in str(exc.orig or exc):
            raise
        existing = session.scalar(
            select(KaspiPayout).where(
                KaspiPayout.ip_id == data.ip_id,
                KaspiPayout.external_payout_id == data.external_payout_id,
            )
        )
        if existing is not None:
            # 200, not the decorator's 201: nothing was created.
            response.status_code = status.HTTP_200_OK
            return {
                "id": existing.id,
                "status": existing.status,
                "idempotent_replay": True,
            }
        raise
    return {"id": payout.id, "status": payout.status}


@router.post("/kaspi/reconcile/{payout_id}", tags=["kaspi"])
def reconcile(
    payout_id: int,
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    amount_tolerance: Decimal = Decimal("1.00"),
    allow_partial: bool = True,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """§44 — full reconciliation across all 8 discrepancy classes (OQ-004)."""
    payout = session.get(KaspiPayout, payout_id)
    if payout is not None:
        ensure_ip_access(session, principal, payout.ip_id)
    from app.schemas import ReconciliationRule

    try:
        report = reconcile_payout_full(
            session,
            payout_id=payout_id,
            rule=ReconciliationRule(amount_tolerance=amount_tolerance, allow_partial=allow_partial),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return report
