"""Production output routes — G-C2."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import _stock_created_by, _stock_error, get_db
from app.core.policy import ensure_ip_access
from app.core.security import require_privilege
from app.models.orders import Order
from app.schemas.inventory import ProductionOutputCreate

router = APIRouter()


@router.post("/production/output", status_code=status.HTTP_201_CREATED, tags=["production"])
def post_production_output(
    data: ProductionOutputCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("SHOP_MANAGER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """G-C2: record a production-output event (quantity only).

    When order_id is given, ip_id is derived from the order (DI-001 safe);
    otherwise ip_id comes from the request body.
    """
    from app.services.inventory import record_production_output

    # Resolve the authoritative IP before the access check: an order-bound
    # output belongs to the order's IP, never to a body-supplied one.
    ip_id: int | None
    if data.order_id is not None:
        order = session.get(Order, data.order_id)
        if order is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"order {data.order_id} not found")
        ip_id = order.ip_id
    else:
        ip_id = data.ip_id
        if ip_id is None:  # unreachable: the schema validator requires ip_id without order_id
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "ip_id is required for make-to-stock output",
            )
    ensure_ip_access(session, principal, ip_id)
    try:
        output_id = record_production_output(
            session,
            ip_id=ip_id,
            product_id=data.product_id,
            quantity=data.quantity,
            order_id=data.order_id,
            produced_at=data.produced_at,
            created_by=_stock_created_by(principal),
            reason=data.reason,
        )
    except ValueError as exc:
        raise _stock_error(exc) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return {"id": output_id, "ip_id": ip_id}


@router.get("/production/finished-goods", tags=["production"])
def get_finished_goods(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    ip_id: int = Query(gt=0),
) -> dict[str, object]:
    """G-C2: finished-goods on-hand per product (v_finished_goods_stock)."""
    ensure_ip_access(session, principal, ip_id)
    rows = session.execute(
        text(
            "SELECT product_id, produced_qty, delivered_qty, on_hand "
            "FROM v_finished_goods_stock WHERE ip_id = :ip ORDER BY product_id"
        ),
        {"ip": ip_id},
    ).all()
    return {
        "ip_id": ip_id,
        "items": [
            {
                "product_id": r.product_id,
                "produced_qty": str(r.produced_qty),
                "delivered_qty": str(r.delivered_qty),
                "on_hand": str(r.on_hand),
            }
            for r in rows
        ],
    }
