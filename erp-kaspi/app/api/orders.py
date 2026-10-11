"""Order routes — Kaspi marketplace orders (§23)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, or_, select, text
from sqlalchemy.orm import Session

from app.api.deps import _decimal_safe, _stock_error, get_db
from app.core.policy import assert_ip_exists, ensure_ip_access, ensure_ip_access_or_all
from app.core.security import require_privilege
from app.models.orders import ORDER_STATUSES, Order, OrderItem
from app.schemas.orders import OrderCreate, OrderMaterialCreate, OrderStatusChange
from app.services import add_order_material

router = APIRouter()


@router.get("/orders", tags=["orders"])
def list_orders(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    status_filter: Literal[
        "NEW", "APPROVED", "ASSEMBLING", "DELIVERING", "COMPLETED", "CANCELLED", "RETURNED"
    ]
    | None = Query(default=None, alias="status"),
    ip_id: int | None = Query(default=None),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E3 — orders list with status filter, search and paging (A15)."""
    allowed = ensure_ip_access_or_all(session, principal)
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        if allowed is not None:
            allowed = [ip_id]

    conditions = ["TRUE"]
    params: dict[str, object] = {}
    if status_filter is not None:
        conditions.append("o.status = :status")
        params["status"] = status_filter
    if allowed is not None:
        conditions.append("o.ip_id = ANY(:allowed)")
        params["allowed"] = allowed
    needle = q.strip()
    if needle:
        conditions.append("(o.external_order_id ILIKE :needle OR o.customer_name ILIKE :needle)")
        params["needle"] = f"%{needle}%"
    where = " AND ".join(conditions)

    base = " FROM orders o JOIN ip_entities ip ON ip.id = o.ip_id WHERE " + where
    total = session.execute(text("SELECT count(*)" + base), params).scalar_one()
    rows = session.execute(
        text(
            "SELECT o.id, o.order_date, o.status, o.source, o.total_amount,"
            " o.kaspi_commission, o.delivery_fee, o.external_order_id,"
            " o.customer_name, o.ip_id, ip.name AS ip_name"
            + base
            + " ORDER BY o.id DESC LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": limit, "offset": offset},
    ).all()
    items = [
        {
            "id": r.id,
            "order_date": r.order_date.date().isoformat(),
            "status": r.status,
            "source": r.source,
            "total_amount": str(r.total_amount),
            "kaspi_commission": str(r.kaspi_commission),
            "delivery_fee": str(r.delivery_fee),
            "external_order_id": r.external_order_id,
            "customer_name": r.customer_name,
            "ip_id": r.ip_id,
            "ip_name": r.ip_name,
        }
        for r in rows
    ]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/orders/statuses", tags=["orders"])
def order_statuses(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
) -> dict[str, object]:
    """TD-016 E3b — the canonical order state machine (chk_orders_status)."""
    del principal
    return {"statuses": list(ORDER_STATUSES)}


@router.get("/orders/summary", tags=["orders"])
def orders_summary(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status", max_length=20),
    q: str = Query(default="", max_length=200),
    ip_id: int | None = Query(default=None),
) -> dict[str, object]:
    """TD-016 E19 — KPI + by-status pie for UI 'Заказы' (replaces A15 KPI SQL)."""
    allowed = ensure_ip_access_or_all(session, principal)
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        if allowed is not None:
            allowed = [ip_id]
    like = f"%{(q or '').strip()}%"

    stmt = select(
        Order.status, Order.total_amount, Order.customer_name, Order.external_order_id, Order.ip_id
    )
    if status_filter is not None:
        stmt = stmt.where(Order.status == status_filter)
    if like != "%%":
        stmt = stmt.where(
            or_(
                Order.customer_name.ilike(like),
                Order.external_order_id.ilike(like),
                Order.id.cast(String) == (q or "").strip(),
            )
        )
    if allowed is not None:
        stmt = stmt.where(Order.ip_id.in_(allowed))
    rows = session.execute(stmt).all()
    by_status: dict[str, int] = {}
    total_amount = Decimal("0")
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        total_amount += r.total_amount
    return {
        "count": len(rows),
        "total_amount": str(total_amount),
        "by_status": [
            {"status": k, "n": v} for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])
        ],
    }


@router.get("/reports/order-costing", tags=["orders"])
def report_order_costing(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    ip_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status", max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E7 — direct costing per order (v_order_costing, §49)."""
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
    if status_filter is not None:
        if status_filter not in ORDER_STATUSES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown status")
        where.append("status = :st")
        params["st"] = status_filter
    base = " FROM v_order_costing WHERE " + " AND ".join(where)
    total = session.execute(text("SELECT count(*)" + base), params).scalar_one()
    rows = session.execute(
        text("SELECT *" + base + " ORDER BY order_id LIMIT :limit OFFSET :offset"),
        params,
    ).mappings()
    return {
        "total": int(total),
        "items": [_decimal_safe(dict(r)) for r in rows],
    }


@router.post("/orders", status_code=status.HTTP_201_CREATED, tags=["orders"])
def post_order(
    data: OrderCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("SHOP_MANAGER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """§23/§47 — create the order AND its items.

    An order whose ``total_amount`` does not equal the sum of its items is
    rejected with 422 (audit #6): an ERP must never silently persist a
    header that contradicts its own lines.
    """
    ensure_ip_access(session, principal, data.ip_id)
    assert_ip_exists(session, data.ip_id)
    items_total = sum((item.unit_price * item.quantity for item in data.items), Decimal("0.00"))
    if items_total != data.total_amount:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"total_amount {data.total_amount} != items_total {items_total}",
        )
    order = Order(
        ip_id=data.ip_id,
        external_order_id=data.external_order_id,
        status=data.status.value,
        total_amount=data.total_amount,
        source=data.source.value,
    )
    session.add(order)
    session.flush()
    for item in data.items:
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=item.product_id,
                product_name=item.product_name,
                quantity=item.quantity,
                unit_price=item.unit_price,
            )
        )
    session.flush()
    return {
        "id": order.id,
        "external_order_id": order.external_order_id,
        "total_amount": str(data.total_amount),
        "items_total": str(items_total),
        "items_saved": len(data.items),
        "consistent": True,
    }


@router.post("/orders/{order_id}/materials", status_code=status.HTTP_201_CREATED, tags=["orders"])
def post_order_material(
    order_id: int,
    data: OrderMaterialCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("SHOP_MANAGER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """P2-INV-002: Record material consumption for an order.

    Price is frozen from materials.current_price (§38). The order's IP
    is used for access control.
    """
    from app.models import Order

    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"order {order_id} not found")
    ensure_ip_access(session, principal, order.ip_id)

    try:
        om = add_order_material(session, order_id, data)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only

    return {
        "id": om.id,
        "order_id": order_id,
        "material_id": om.material_id,
        "quantity": str(om.quantity),
        "price_at_time": str(om.price_at_time),
        "total_cost": str(om.total_cost),
    }


@router.patch("/orders/{order_id}/status", tags=["orders"])
def patch_order_status(
    order_id: int,
    data: OrderStatusChange,
    principal: Annotated[dict[str, object], Depends(require_privilege("SHOP_MANAGER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Change an order's status. CANCELLED releases ACTIVE reservations."""
    from app.models import Order
    from app.services.inventory import set_order_status

    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"order {order_id} not found")
    ensure_ip_access(session, principal, order.ip_id)
    try:
        order = set_order_status(session, order_id, data.status)
    except ValueError as exc:
        raise _stock_error(exc) from exc
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return {"id": order.id, "status": order.status}
