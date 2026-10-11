"""
Inventory quantity ledger models (P2-INV-001..004).

1:1 mapping of database/schema.sql SECTION 3c:
  stock_movements       [ARCHITECTURAL EXTENSION, P2-INV-001]
  material_reservations [ARCHITECTURAL EXTENSION, P2-INV-003]

Behaviour that lives in the database, not here:
  * stock_movements rows are immutable: BEFORE UPDATE/DELETE trigger
    fn_stock_movements_immutable() blocks every mutation (DI-005 STRONG
    pattern — GUC app.allow_financial_delete + erp_migrator membership).
    Corrections are compensating movements (§5), never UPDATEs.
  * fn_stock_movements_integrity() serializes per-material inserts
    (FOR UPDATE on the catalog row), enforces DI-001 (movement IP ==
    order IP), and blocks movements that would drive on-hand negative.
  * uq_stock_movements_ref (ref_type, ref_id) makes retried writes
    idempotent (F-ETL-008 pattern); the service checks existence first.
  * material_reservations is mutable by design (lifecycle) — only
    fn_reservations_ip() (DI-001) is trigger-enforced. The CANCELLED →
    release hook lives in the service layer, not a trigger.
"""

from __future__ import annotations

import datetime
import decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import (
    TIMESTAMPTZ,
    Base,
    created_at_column,
    pk_column,
    quantity_column,
)

if TYPE_CHECKING:
    pass


#: The 7 ledger movement types (chk_stock_movements_type).
STOCK_MOVEMENT_TYPES: tuple[str, ...] = (
    "RECEIPT",
    "ISSUE",
    "PRODUCTION_CONSUMPTION",
    "PRODUCTION_OUTPUT",
    "RETURN",
    "ADJUSTMENT",
    "SCRAP",
)

#: Positive-delta types.
_STOCK_POSITIVE_TYPES = "('RECEIPT','PRODUCTION_OUTPUT')"
#: Negative-delta types.
_STOCK_NEGATIVE_TYPES = "('ISSUE','PRODUCTION_CONSUMPTION','RETURN','SCRAP')"


class StockMovement(Base):
    """ARCHITECTURAL EXTENSION — immutable quantity ledger row (P2-INV-001)."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint("quantity_delta <> 0", name="chk_stock_movements_qty_nonzero"),
        CheckConstraint(
            "movement_type IN "
            "('RECEIPT','ISSUE','PRODUCTION_CONSUMPTION',"
            "'PRODUCTION_OUTPUT','RETURN','ADJUSTMENT','SCRAP')",
            name="chk_stock_movements_type",
        ),
        CheckConstraint(
            f"(movement_type IN {_STOCK_POSITIVE_TYPES} AND quantity_delta > 0)"
            f" OR (movement_type IN {_STOCK_NEGATIVE_TYPES} AND quantity_delta < 0)"
            " OR (movement_type = 'ADJUSTMENT')",
            name="chk_stock_movements_sign",
        ),
        CheckConstraint(
            "movement_type NOT IN ('ADJUSTMENT', 'SCRAP')"
            " OR (reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name="chk_stock_movements_reason_required",
        ),
        CheckConstraint(
            "(movement_type NOT IN ('PRODUCTION_CONSUMPTION', 'PRODUCTION_OUTPUT'))"
            " OR order_id IS NOT NULL",
            name="chk_stock_movements_order_scope",
        ),
        CheckConstraint(
            "movement_type <> 'RETURN' OR counterparty_id IS NOT NULL",
            name="chk_stock_movements_return_counterparty",
        ),
        Index(
            "uq_stock_movements_ref",
            "ref_type",
            "ref_id",
            unique=True,
            postgresql_where="ref_type IS NOT NULL AND ref_id IS NOT NULL",
        ),
        Index("ix_stock_movements_ip_material", "ip_id", "material_id", "movement_date"),
        Index(
            "ix_stock_movements_order",
            "order_id",
            postgresql_where="order_id IS NOT NULL",
        ),
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ip_entities.id", ondelete="RESTRICT"), nullable=False
    )
    material_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=True
    )
    counterparty_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("counterparties.id", ondelete="SET NULL"),
        nullable=True,
    )
    movement_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    quantity_delta: Mapped[decimal.Decimal] = quantity_column()
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = created_at_column()


class MaterialReservation(Base):
    """ARCHITECTURAL EXTENSION — stock reservation, an intention (P2-INV-003).

    NOT a ledger row: reserving does not move physical stock, so it must
    not appear in SUM(quantity_delta). Availability = on-hand minus ACTIVE,
    unexpired reservations (v_material_availability, expiry read-time).
    """

    __tablename__ = "material_reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_reservations_qty"),
        CheckConstraint(
            "status IN ('ACTIVE', 'RELEASED', 'CONSUMED')",
            name="chk_reservations_status",
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND released_at IS NULL)"
            " OR (status <> 'ACTIVE' AND released_at IS NOT NULL)",
            name="chk_reservations_release",
        ),
        Index(
            "ix_reservations_ip_material",
            "ip_id",
            "material_id",
            postgresql_where="status = 'ACTIVE'",
        ),
        Index(
            "ix_reservations_order",
            "order_id",
            postgresql_where="status = 'ACTIVE'",
        ),
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ip_entities.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    material_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[decimal.Decimal] = quantity_column()
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    reserved_at: Mapped[datetime.datetime] = created_at_column()
    expires_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    released_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    released_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ProductionOutput(Base):
    """ARCHITECTURAL EXTENSION — quantity-only production output (G-C2).

    The missing WIP → finished-goods link: one row per production event.
    order_id NULL = make-to-stock (production without a sales order).

    NO unit_cost, NO valuation (O-C1 owner-blocked) — quantity only.

    Behaviour that lives in the database, not here:
    * Rows are immutable: BEFORE UPDATE/DELETE trigger
      fn_production_output_immutable() blocks every mutation (DI-005 STRONG
      pattern — GUC app.allow_financial_delete + erp_migrator membership).
      Corrections are compensating rows, never UPDATEs.
    * fn_production_output_ip() enforces DI-001: when order_id is set,
      output IP must equal the order's IP.
    * v_finished_goods_stock derives on-hand per (IP, product).
    """

    __tablename__ = "production_output"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_production_output_qty"),
        Index("ix_production_output_ip_product", "ip_id", "product_id"),
        Index(
            "ix_production_output_order",
            "order_id",
            postgresql_where="order_id IS NOT NULL",
        ),
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ip_entities.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=True
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[decimal.Decimal] = quantity_column()
    produced_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
