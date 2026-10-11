"""
Orders, order lines and material consumption models.

1:1 mapping of database/schema.sql:
  orders               [ORIGINAL #6, §23, §24]
  order_status_history [ARCHITECTURAL EXTENSION, §50]
  order_items          [ARCHITECTURAL EXTENSION, §47]
  order_materials      [ARCHITECTURAL EXTENSION, §49]

Behaviour that lives in the database, not here:
  * §24 — creating an order produces NO financial operation; money is
    recognised only through the payout registry;
  * §50 — order_status_history rows are written by
    fn_log_order_status_change();
  * §38 — order_materials.price_at_time is frozen on INSERT by
    fn_order_material_freeze_price() and immutable afterwards.
"""

from __future__ import annotations

import datetime
import decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import (
    MONEY,
    TIMESTAMPTZ,
    Base,
    created_at_column,
    money_column,
    pk_column,
    quantity_column,
    updated_at_column,
)

if TYPE_CHECKING:
    from app.models.hr import WorkEntry
    from app.models.kaspi import RawKaspiPayload
    from app.models.reference import IpEntity, Material, Product

#: The seven order statuses of §23.
ORDER_STATUSES: tuple[str, ...] = (
    "NEW",
    "APPROVED",
    "ASSEMBLING",
    "DELIVERING",
    "COMPLETED",
    "CANCELLED",
    "RETURNED",
)

_ORDER_STATUS_SQL_LIST = ", ".join(f"'{status}'" for status in ORDER_STATUSES)


class Order(Base):
    """
    Customer order from Kaspi Shop, Kaspi Pay, Excel or manual entry (§23).

    §24 — a Kaspi Shop order is NOT a bank inflow at creation time.
    """

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(f"status IN ({_ORDER_STATUS_SQL_LIST})", name="chk_orders_status"),
        CheckConstraint(
            "source IN ('KASPI_SHOP', 'KASPI_PAY', 'MANUAL', 'EXCEL')",
            name="chk_orders_source",
        ),
        CheckConstraint("total_amount >= 0", name="chk_orders_total_amount"),
        CheckConstraint("kaspi_commission >= 0", name="chk_orders_kaspi_commission"),
        CheckConstraint("delivery_fee >= 0", name="chk_orders_delivery_fee"),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= order_date",
            name="chk_orders_completed_at",
        ),
        # §23 — UNIQUE (ip_id, external_order_id).
        UniqueConstraint("ip_id", "external_order_id", name="uq_orders_ip_external"),
        # §64 — mandatory index name.
        Index("idx_orders_status", "ip_id", "status", "created_at"),
        Index("idx_orders_order_date", "order_date"),
        Index("idx_orders_source", "source", "status"),
        Index("idx_orders_raw_payload", "raw_payload_id"),
        Index(
            "idx_orders_customer_phone",
            "customer_phone",
            postgresql_where=text("customer_phone IS NOT NULL"),
        ),
        {
            "comment": "Customer orders from Kaspi Shop, Kaspi Pay, Excel or manual "
            "entry (MASTER PROMPT §23)."
        },
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_order_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Order id in the source system; UNIQUE per ИП (§23). "
        "NULL for purely internal orders.",
    )
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'NEW'"),
    )
    total_amount: Mapped[decimal.Decimal] = money_column(
        default_zero=True,
        comment="Order revenue in KZT as reported by the source. Not recognised as "
        "cash until payout (§24).",
    )
    kaspi_commission: Mapped[decimal.Decimal] = money_column(
        default_zero=True,
        comment="Commission as reported by Kaspi. Not computed by this system — "
        "the rule is unknown (OQ-005, §94).",
    )
    delivery_fee: Mapped[decimal.Decimal] = money_column(default_zero=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()
    # production additions (§81)
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'MANUAL'"),
    )
    order_date: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMPTZ,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # FK fk_orders_raw_payload is created in SECTION 5 of schema.sql
    # (circular dependency orders <-> raw_kaspi_payloads).
    raw_payload_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "raw_kaspi_payloads.id",
            ondelete="SET NULL",
            name="fk_orders_raw_payload",
            use_alter=True,
        ),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    ip_entity: Mapped[IpEntity] = relationship()
    raw_payload: Mapped[RawKaspiPayload | None] = relationship(
        back_populates="orders",
        foreign_keys=[raw_payload_id],
    )
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )
    materials: Mapped[list[OrderMaterial]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )
    status_history: Mapped[list[OrderStatusHistory]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )
    work_entries: Mapped[list[WorkEntry]] = relationship(
        back_populates="order",
        passive_deletes="all",
    )


class OrderStatusHistory(Base):
    """ARCHITECTURAL EXTENSION — every order status transition (§50)."""

    __tablename__ = "order_status_history"
    __table_args__ = (
        CheckConstraint(
            f"new_status IN ({_ORDER_STATUS_SQL_LIST})",
            name="chk_order_status_history_new",
        ),
        CheckConstraint(
            f"old_status IS NULL OR old_status IN ({_ORDER_STATUS_SQL_LIST})",
            name="chk_order_status_history_old",
        ),
        CheckConstraint(
            "old_status IS DISTINCT FROM new_status",
            name="chk_order_status_history_change",
        ),
        Index("idx_order_status_history_order", "order_id", "changed_at"),
        {"comment": "ARCHITECTURAL EXTENSION — every order status transition (MASTER PROMPT §50)."},
    )

    id: Mapped[int] = pk_column()
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    old_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_status: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_at: Mapped[datetime.datetime] = created_at_column()
    changed_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    order: Mapped[Order] = relationship(back_populates="status_history")


class OrderItem(Base):
    """
    ARCHITECTURAL EXTENSION — order line with historical price (§47).

    total_amount is GENERATED: round(quantity*unit_price, 2) - discount.
    """

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_order_items_quantity"),
        CheckConstraint("unit_price >= 0", name="chk_order_items_unit_price"),
        CheckConstraint(
            "discount >= 0 AND discount <= round(quantity * unit_price, 2)",
            name="chk_order_items_discount",
        ),
        Index("idx_order_items_order", "order_id"),
        Index("idx_order_items_product", "product_id"),
        {
            "comment": "ARCHITECTURAL EXTENSION — order lines with historical price "
            "(MASTER PROMPT §47)."
        },
    )

    id: Mapped[int] = pk_column()
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=True,
    )
    quantity: Mapped[decimal.Decimal] = quantity_column()
    unit_price: Mapped[decimal.Decimal] = mapped_column(
        MONEY,
        nullable=False,
        comment="Price at the moment of sale — historical, never re-read from products (§47).",
    )
    discount: Mapped[decimal.Decimal] = money_column(default_zero=True)
    total_amount: Mapped[decimal.Decimal] = mapped_column(
        MONEY,
        Computed("round(quantity * unit_price, 2) - discount", persisted=True),
        nullable=True,
        comment="GENERATED: round(quantity*unit_price,2) - discount (§47).",
    )
    # production additions (§81)
    product_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Denormalised product name snapshot for external items with no catalogue entry.",
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product | None] = relationship()


class OrderMaterial(Base):
    """
    ARCHITECTURAL EXTENSION — materials consumed by an order (§49).

    price_at_time is frozen at the moment of use and immutable (§38).
    total_cost is GENERATED: round(quantity*price_at_time, 2).
    """

    __tablename__ = "order_materials"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_order_materials_quantity"),
        CheckConstraint("price_at_time >= 0", name="chk_order_materials_price_at_time"),
        Index("idx_order_materials_order", "order_id"),
        Index("idx_order_materials_material", "material_id"),
        {
            "comment": "ARCHITECTURAL EXTENSION — materials consumed by an order "
            "(MASTER PROMPT §49)."
        },
    )

    id: Mapped[int] = pk_column()
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    material_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("materials.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity: Mapped[decimal.Decimal] = quantity_column()
    price_at_time: Mapped[decimal.Decimal] = mapped_column(
        MONEY,
        nullable=False,
        comment="FROZEN material price at the moment of use; immutable (§38, ADR-008).",
    )
    total_cost: Mapped[decimal.Decimal] = mapped_column(
        MONEY,
        Computed("round(quantity * price_at_time, 2)", persisted=True),
        nullable=True,
        comment="GENERATED: round(quantity*price_at_time,2) — the material part "
        "of direct cost (§37).",
    )
    # production additions (§81)
    used_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMPTZ,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    order: Mapped[Order] = relationship(back_populates="materials")
    material: Mapped[Material] = relationship()


__all__ = [
    "ORDER_STATUSES",
    "Order",
    "OrderItem",
    "OrderMaterial",
    "OrderStatusHistory",
]
