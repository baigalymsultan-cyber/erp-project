"""Inventory / stock movement routes — P2-INV-001/002/003."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import _stock_created_by, _stock_error, get_db
from app.core.policy import ensure_ip_access
from app.core.security import require_privilege
from app.schemas.inventory import (
    ReservationCreate,
    ReservationReleaseCreate,
    StockAdjustmentCreate,
    StockIssueCreate,
    StockReceiptCreate,
    StockScrapCreate,
)

router = APIRouter()




@router.post("/stock/receipts", status_code=status.HTTP_201_CREATED, tags=["stock"])
def post_stock_receipt(
    data: StockReceiptCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("SHOP_MANAGER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """P2-INV-001: stock IN — supplier delivery, customer return to stock."""
    from app.services.inventory import record_movement

    ensure_ip_access(session, principal, data.ip_id)
    try:
        movement_id = record_movement(
            session,
            ip_id=data.ip_id,
            material_id=data.material_id,
            quantity_delta=data.quantity,
            movement_type="RECEIPT",
            movement_date=data.movement_date,
            counterparty_id=data.counterparty_id,
            reason=data.reason,
            created_by=_stock_created_by(principal),
        )
    except ValueError as exc:
        raise _stock_error(exc) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return {"id": movement_id, "movement_type": "RECEIPT"}


@router.post("/stock/issues", status_code=status.HTTP_201_CREATED, tags=["stock"])
def post_stock_issue(
    data: StockIssueCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("SHOP_MANAGER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """P2-INV-001: stock OUT — general issue NOT tied to a production order."""
    from app.services.inventory import record_movement

    ensure_ip_access(session, principal, data.ip_id)
    try:
        movement_id = record_movement(
            session,
            ip_id=data.ip_id,
            material_id=data.material_id,
            quantity_delta=-data.quantity,
            movement_type="ISSUE",
            movement_date=data.movement_date,
            reason=data.reason,
            created_by=_stock_created_by(principal),
        )
    except ValueError as exc:
        raise _stock_error(exc) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return {"id": movement_id, "movement_type": "ISSUE"}


@router.post("/stock/adjustments", status_code=status.HTTP_201_CREATED, tags=["stock"])
def post_stock_adjustment(
    data: StockAdjustmentCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("SHOP_MANAGER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """P2-INV-001: signed correction from a physical count (reason required)."""
    from app.services.inventory import record_movement

    ensure_ip_access(session, principal, data.ip_id)
    try:
        movement_id = record_movement(
            session,
            ip_id=data.ip_id,
            material_id=data.material_id,
            quantity_delta=data.quantity_delta,
            movement_type="ADJUSTMENT",
            movement_date=data.movement_date,
            reason=data.reason,
            created_by=_stock_created_by(principal),
        )
    except ValueError as exc:
        raise _stock_error(exc) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return {"id": movement_id, "movement_type": "ADJUSTMENT"}


@router.post("/stock/scrap", status_code=status.HTTP_201_CREATED, tags=["stock"])
def post_stock_scrap(
    data: StockScrapCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("SHOP_MANAGER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """P2-INV-001: waste/offcuts — always negative (reason required)."""
    from app.services.inventory import record_movement

    ensure_ip_access(session, principal, data.ip_id)
    try:
        movement_id = record_movement(
            session,
            ip_id=data.ip_id,
            material_id=data.material_id,
            quantity_delta=-data.quantity,
            movement_type="SCRAP",
            movement_date=data.movement_date,
            reason=data.reason,
            created_by=_stock_created_by(principal),
        )
    except ValueError as exc:
        raise _stock_error(exc) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return {"id": movement_id, "movement_type": "SCRAP"}


@router.get("/stock/levels", tags=["stock"])
def get_stock_levels(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    ip_id: int = Query(gt=0),
) -> dict[str, object]:
    """P2-INV-001: physical on-hand per material (v_material_stock)."""
    ensure_ip_access(session, principal, ip_id)
    rows = session.execute(
        text(
            "SELECT material_id, on_hand, movement_count FROM v_material_stock "
            "WHERE ip_id = :ip ORDER BY material_id"
        ),
        {"ip": ip_id},
    ).all()
    return {
        "ip_id": ip_id,
        "items": [
            {
                "material_id": r.material_id,
                "on_hand": str(r.on_hand),
                "movement_count": r.movement_count,
            }
            for r in rows
        ],
    }


@router.get("/stock/availability", tags=["stock"])
def get_stock_availability(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    ip_id: int = Query(gt=0),
) -> dict[str, object]:
    """P2-INV-003: on-hand minus active, unexpired reservations."""
    ensure_ip_access(session, principal, ip_id)
    rows = session.execute(
        text(
            "SELECT material_id, on_hand, reserved_qty, available_qty "
            "FROM v_material_availability WHERE ip_id = :ip ORDER BY material_id"
        ),
        {"ip": ip_id},
    ).all()
    return {
        "ip_id": ip_id,
        "items": [
            {
                "material_id": r.material_id,
                "on_hand": str(r.on_hand),
                "reserved_qty": str(r.reserved_qty),
                "available_qty": str(r.available_qty),
            }
            for r in rows
        ],
    }


@router.post("/stock/reservations", status_code=status.HTTP_201_CREATED, tags=["stock"])
def post_stock_reservation(
    data: ReservationCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("SHOP_MANAGER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """P2-INV-003: reserve stock for an order. Over-reserve warns, not blocks."""
    from app.models import Order
    from app.services.inventory import reserve_stock

    order = session.get(Order, data.order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"order {data.order_id} not found")
    ensure_ip_access(session, principal, order.ip_id)
    try:
        result = reserve_stock(
            session,
            order_id=data.order_id,
            material_id=data.material_id,
            quantity=data.quantity,
            expires_at=data.expires_at,
            created_by=_stock_created_by(principal),
        )
    except ValueError as exc:
        raise _stock_error(exc) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return result


@router.post("/stock/reservations/{reservation_id}/release", tags=["stock"])
def post_stock_reservation_release(
    reservation_id: int,
    data: ReservationReleaseCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("SHOP_MANAGER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """P2-INV-003: release an ACTIVE reservation."""
    from app.models import MaterialReservation
    from app.services.inventory import release_reservation

    reservation = session.get(MaterialReservation, reservation_id)
    if reservation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"reservation {reservation_id} not found")
    ensure_ip_access(session, principal, reservation.ip_id)
    try:
        result = release_reservation(session, reservation_id, data.reason)
    except ValueError as exc:
        raise _stock_error(exc) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return result
