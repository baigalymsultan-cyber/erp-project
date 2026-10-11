"""Inventory quantity ledger services (P2-INV-001..004).

Transport-agnostic: take a Session, return ids/dicts. No HTTP, no commits —
the caller owns the transaction. The database (triggers + CHECKs) is the
last line of defense; Python-side validation runs BEFORE flush() so
violations surface as ValueError (→ 422), not DB check_violation (→ 500).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.inventory import (
    STOCK_MOVEMENT_TYPES,
    MaterialReservation,
    ProductionOutput,
    StockMovement,
)
from app.models.orders import ORDER_STATUSES, Order

#: Reservation release reasons (chk not DB-enforced; service-validated).
RESERVATION_RELEASE_REASONS: tuple[str, ...] = (
    "ORDER_CANCELLED",
    "MANUAL",
    "EXPIRED",
    "SUPERSEDED",
)

_POSITIVE_TYPES = frozenset({"RECEIPT", "PRODUCTION_OUTPUT"})
_NEGATIVE_TYPES = frozenset({"ISSUE", "PRODUCTION_CONSUMPTION", "RETURN", "SCRAP"})


def record_movement(
    session: Session,
    *,
    ip_id: int,
    material_id: int,
    quantity_delta: Decimal,
    movement_type: str,
    movement_date: datetime.date | None = None,
    order_id: int | None = None,
    counterparty_id: int | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    reason: str | None = None,
    created_by: int | None = None,
) -> int:
    """Write one immutable ledger row; return its id.

    Idempotency: when (ref_type, ref_id) is supplied, an existing row is
    returned instead of inserting (protects retried API calls; the partial
    unique index uq_stock_movements_ref is the backstop).
    """
    # --- Python-side validation BEFORE flush (ValueError → 422) ---------
    if movement_type not in STOCK_MOVEMENT_TYPES:
        raise ValueError(f"unknown movement_type {movement_type!r}")
    quantity_delta = Decimal(quantity_delta)
    if quantity_delta == 0:
        raise ValueError("quantity_delta must be non-zero")
    if movement_type in _POSITIVE_TYPES and quantity_delta <= 0:
        raise ValueError(f"{movement_type} requires a positive quantity_delta")
    if movement_type in _NEGATIVE_TYPES and quantity_delta >= 0:
        raise ValueError(f"{movement_type} requires a negative quantity_delta")
    day = movement_date or datetime.date.today()
    if day > datetime.date.today():
        raise ValueError("movement_date cannot be in the future")
    if movement_type in ("ADJUSTMENT", "SCRAP") and not (reason or "").strip():
        raise ValueError(f"{movement_type} requires a non-blank reason")
    if movement_type in ("PRODUCTION_CONSUMPTION", "PRODUCTION_OUTPUT") and order_id is None:
        raise ValueError(f"{movement_type} requires order_id")
    if movement_type == "RETURN" and counterparty_id is None:
        raise ValueError("RETURN requires counterparty_id")

    # --- Idempotency first ----------------------------------------------
    if ref_type is not None and ref_id is not None:
        existing = session.execute(
            select(StockMovement.id).where(
                StockMovement.ref_type == ref_type,
                StockMovement.ref_id == ref_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)

    movement = StockMovement(
        ip_id=ip_id,
        material_id=material_id,
        order_id=order_id,
        counterparty_id=counterparty_id,
        movement_date=day,
        quantity_delta=quantity_delta,
        movement_type=movement_type,
        ref_type=ref_type,
        ref_id=ref_id,
        reason=(reason.strip() if reason else None),
        created_by=created_by,
    )
    session.add(movement)
    session.flush()  # DB constraints/trigger are the last line of defense
    return int(movement.id)


def reserve_stock(
    session: Session,
    *,
    order_id: int,
    material_id: int,
    quantity: Decimal,
    expires_at: datetime.datetime | None = None,
    created_by: int | None = None,
) -> dict[str, object]:
    """Reserve stock for an order. Returns {"reservation_id", "warning"?}.

    Policy: over-reserve is a WARNING, not a hard block (visibility-first;
    hard-block is an owner decision). The reservation inherits the order's
    IP (DI-001); the trigger re-verifies it.
    """
    order = session.get(Order, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise ValueError("reservation quantity must be positive")
    if expires_at is not None and expires_at.tzinfo is None:
        raise ValueError(
            "expires_at must be timezone-aware (DB session is UTC; "
            "a naive datetime would be misinterpreted)"
        )

    # Availability from the read model (on-hand minus active, unexpired
    # reservations). Missing row = no stock movements yet → 0 available.
    available_qty = session.execute(
        text(
            "SELECT available_qty FROM v_material_availability "
            "WHERE ip_id = :ip AND material_id = :m"
        ),
        {"ip": order.ip_id, "m": material_id},
    ).scalar_one_or_none()
    available_qty = Decimal(available_qty) if available_qty is not None else Decimal("0")

    reservation = MaterialReservation(
        ip_id=order.ip_id,
        order_id=order_id,
        material_id=material_id,
        quantity=quantity,
        expires_at=expires_at,
        created_by=created_by,
    )
    session.add(reservation)
    session.flush()

    result: dict[str, object] = {"reservation_id": int(reservation.id)}
    if quantity > available_qty:
        result["warning"] = "over_reserved"
        result["available_qty"] = str(available_qty)
    return result


def release_reservation(
    session: Session,
    reservation_id: int,
    reason: str,
) -> dict[str, object]:
    """Release an ACTIVE reservation. Returns {"reservation_id", "status"}."""
    if reason not in RESERVATION_RELEASE_REASONS:
        raise ValueError(f"unknown release reason {reason!r}")
    reservation = session.get(MaterialReservation, reservation_id)
    if reservation is None:
        raise ValueError(f"reservation {reservation_id} not found")
    if reservation.status != "ACTIVE":
        raise ValueError(f"reservation {reservation_id} is {reservation.status}, not ACTIVE")
    reservation.status = "RELEASED"
    reservation.released_at = datetime.datetime.now(datetime.UTC)
    reservation.released_reason = reason
    session.flush()
    return {"reservation_id": int(reservation.id), "status": "RELEASED"}


def release_order_reservations(
    session: Session,
    order_id: int,
    reason: str = "ORDER_CANCELLED",
) -> int:
    """Release every ACTIVE reservation of an order. Returns the count."""
    if reason not in RESERVATION_RELEASE_REASONS:
        raise ValueError(f"unknown release reason {reason!r}")
    now = datetime.datetime.now(datetime.UTC)
    rows = session.execute(
        select(MaterialReservation).where(
            MaterialReservation.order_id == order_id,
            MaterialReservation.status == "ACTIVE",
        )
    ).scalars()
    count = 0
    for reservation in rows:
        reservation.status = "RELEASED"
        reservation.released_at = now
        reservation.released_reason = reason
        count += 1
    session.flush()
    return count


def consume_reservations(
    session: Session,
    *,
    order_id: int,
    material_id: int,
    quantity: Decimal,
) -> Decimal:
    """Mark ACTIVE reservations CONSUMED (FIFO by reserved_at).

    Called by add_order_material(): the consumed portion of a reservation
    stops counting as reserved. Partially consumed rows keep the remainder
    ACTIVE with a reduced quantity. Returns the consumed amount.
    """
    remaining = Decimal(quantity)
    if remaining <= 0:
        return Decimal("0")
    rows = (
        session.execute(
            select(MaterialReservation)
            .where(
                MaterialReservation.order_id == order_id,
                MaterialReservation.material_id == material_id,
                MaterialReservation.status == "ACTIVE",
            )
            .order_by(MaterialReservation.reserved_at, MaterialReservation.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    now = datetime.datetime.now(datetime.UTC)
    consumed = Decimal("0")
    for reservation in rows:
        if remaining <= 0:
            break
        take = min(Decimal(reservation.quantity), remaining)
        consumed += take
        remaining -= take
        rest = Decimal(reservation.quantity) - take
        if rest <= 0:
            reservation.status = "CONSUMED"
            reservation.released_at = now
            reservation.released_reason = "CONSUMED"
        else:
            reservation.quantity = rest  # stays ACTIVE with the remainder
    session.flush()
    return consumed


def set_order_status(
    session: Session,
    order_id: int,
    new_status: str,
) -> Order:
    """Change an order's status. CANCELLED releases ACTIVE reservations.

    The service-layer hook (not a DB trigger) — business flow stays in
    Python, consistent with the codebase. fn_log_order_status_change()
    records the transition in order_status_history.
    """
    if new_status not in ORDER_STATUSES:
        raise ValueError(f"unknown order status {new_status!r}")
    order = session.get(Order, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    if order.status == new_status:
        return order
    order.status = new_status
    session.flush()
    if new_status == "CANCELLED":
        release_order_reservations(session, order_id, reason="ORDER_CANCELLED")
    return order


def record_production_output(
    session: Session,
    *,
    ip_id: int,
    product_id: int,
    quantity: Decimal,
    order_id: int | None = None,
    produced_at: datetime.datetime | None = None,
    created_by: int | None = None,
    reason: str | None = None,
) -> int:
    """Record one production-output event; return its id (G-C2).

    Quantity only — no unit_cost, no valuation (O-C1 owner-blocked).
    order_id NULL = make-to-stock (production without a sales order).

    When order_id is given, the IP is derived from the order (DI-001
    safe); the trigger re-verifies it. Otherwise ip_id comes from the
    caller (API layer: ensure_ip_access). produced_at must be
    timezone-aware — a naive datetime would be misinterpreted by the
    UTC DB session (same hardening as reserve_stock).
    """
    from app.models.reference import Product

    quantity = Decimal(quantity)
    if quantity <= 0:
        raise ValueError("production quantity must be positive")
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError(f"product {product_id} not found")
    if order_id is not None:
        order = session.get(Order, order_id)
        if order is None:
            raise ValueError(f"order {order_id} not found")
        ip_id = order.ip_id  # authoritative: DI-001, trigger re-verifies
    if produced_at is not None and produced_at.tzinfo is None:
        raise ValueError(
            "produced_at must be timezone-aware (DB session is UTC; "
            "a naive datetime would be misinterpreted)"
        )

    output = ProductionOutput(
        ip_id=ip_id,
        order_id=order_id,
        product_id=product_id,
        quantity=quantity,
        produced_at=produced_at or datetime.datetime.now(datetime.UTC),
        created_by=created_by,
        reason=(reason.strip() if reason else None),
    )
    session.add(output)
    session.flush()  # DB constraints/triggers are the last line of defense
    return int(output.id)
