"""Order domain services — costing, materials."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.hr import WorkEntry
from app.models.orders import Order, OrderMaterial
from app.models.reference import Material
from app.schemas import OrderCreate, OrderMaterialCreate
from app.services.common import _q2
from app.services.inventory import consume_reservations, record_movement


def order_direct_cost(session: Session, order_id: int) -> dict[str, Decimal]:
    """Direct cost of an order: materials + piecework labour (§37).

    Overhead (rent, utilities, ads) is NOT allocated here (OQ-010 —
    deferred until the allocation rule is answered).

    Labour counts CONFIRMED/PAID work entries only (G-C4) — the same
    filter as v_order_costing, so the Python API and the view agree.
    DRAFT entries are not cost.
    """
    # §49/§38 — material cost comes from order_materials, whose price_at_time
    # is the frozen purchase price of what the order actually consumed.
    # unit_price on order_items is the SELLING price; using it here used to
    # value materials at revenue (audit #7).
    material_cost = session.scalar(
        select(func.coalesce(func.sum(OrderMaterial.total_cost), 0)).where(
            OrderMaterial.order_id == order_id
        )
    ) or Decimal("0")
    labour_cost = session.scalar(
        select(func.coalesce(func.sum(WorkEntry.total_sum), 0)).where(
            WorkEntry.order_id == order_id,
            # G-C4: align with v_order_costing — DRAFT entries are not cost.
            WorkEntry.status.in_(["CONFIRMED", "PAID"]),
        )
    ) or Decimal("0")
    return {
        "material_cost": _q2(Decimal(material_cost)),
        "labor_cost": _q2(Decimal(labour_cost)),
        "total_direct_cost": _q2(Decimal(material_cost) + Decimal(labour_cost)),
    }


def add_order_material(
    session: Session,
    order_id: int,
    data: OrderMaterialCreate,
    created_by: int | None = None,
) -> OrderMaterial:
    """P2-INV-002: Record material consumption for an order.

    Price is FROZEN from materials.current_price at insertion time (§38).
    Client cannot supply price_at_time (prevents tampering).

    P2-INV-001: mirrors the consumption into the quantity ledger in the
    SAME transaction — ip_id comes from the ORDER (never the request),
    and a negative-stock gate failure rolls back both rows. Matching
    ACTIVE reservations are consumed FIFO (oldest first).
    """
    order = session.get(Order, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")

    material = session.get(Material, data.material_id)
    if material is None:
        raise ValueError(f"material {data.material_id} not found")

    om = OrderMaterial(
        order_id=order_id,
        material_id=data.material_id,
        quantity=data.quantity,
        price_at_time=material.current_price,  # §38: frozen
        created_by=created_by,
    )
    session.add(om)
    session.flush()  # Populate id and total_cost (GENERATED)
    session.refresh(om)

    # Ledger mirror: same transaction, order's IP (DI-001 safe).
    record_movement(
        session,
        ip_id=order.ip_id,
        material_id=data.material_id,
        quantity_delta=-Decimal(data.quantity),
        movement_type="PRODUCTION_CONSUMPTION",
        movement_date=om.used_at.date(),
        order_id=order_id,
        ref_type="ORDER_MATERIALS",
        ref_id=om.id,
        created_by=created_by,
    )
    consume_reservations(
        session,
        order_id=order_id,
        material_id=data.material_id,
        quantity=Decimal(data.quantity),
    )
    return om


def order_gross_profit(
    session: Session, order: Order, direct_cost: Decimal
) -> dict[str, Decimal | float]:
    """Gross profit and margin percent of an order (§37)."""
    gross = _q2(Decimal(order.total_amount) - direct_cost)
    margin = float(gross) / float(order.total_amount) * 100 if order.total_amount else 0.0
    return {"gross_profit": gross, "margin_percent": _q2(Decimal(f"{margin:.2f}"))}


def kaspi_gross_vs_items(session: Session, order_create: OrderCreate, items_total: Decimal) -> bool:
    """§23: order total must equal the sum of its items."""
    return Decimal(order_create.total_amount) == Decimal(items_total)
