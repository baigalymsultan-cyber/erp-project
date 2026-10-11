"""Order + Kaspi order schemas — §23."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Money, OrderStatus, PaymentMethod, Quantity

# --------------------------------------------------------------------------- #
# Orders — Kaspi marketplace orders (§23: UNIQUE (ip_id, external_order_id))
# --------------------------------------------------------------------------- #


class OrderItemCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    product_id: int | None = Field(default=None, gt=0)
    product_name: str = Field(min_length=1, max_length=200)
    quantity: Quantity = Field(gt=0)
    unit_price: Money = Field(gt=0)


class OrderMaterialCreate(BaseModel):
    """P2-INV-002: Record material consumption for an order."""

    model_config = ConfigDict(str_strip_whitespace=True)

    material_id: int = Field(gt=0)
    quantity: Quantity = Field(gt=0)


class OrderCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    ip_id: int = Field(gt=0)
    external_order_id: str = Field(min_length=1, max_length=64)
    status: OrderStatus = OrderStatus.NEW
    total_amount: Money = Field(gt=0)
    source: PaymentMethod
    items: list[OrderItemCreate] = Field(min_length=1)


class OrderRead(OrderCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class OrderStatusChange(BaseModel):
    """Change an order's status. CANCELLED releases ACTIVE reservations."""

    status: Literal[
        "NEW", "APPROVED", "ASSEMBLING", "DELIVERING", "COMPLETED", "CANCELLED", "RETURNED"
    ]
