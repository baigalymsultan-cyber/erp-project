"""Inventory / stock / production schemas — P2-INV-001/002/003, G-C2."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import Quantity

    # price_at_time is FROZEN from materials.current_price (§38) —
    # not accepted from client to prevent tampering.


class StockReceiptCreate(BaseModel):
    """P2-INV-001: stock IN — supplier delivery, customer return to stock."""

    model_config = ConfigDict(str_strip_whitespace=True)

    ip_id: int = Field(gt=0)
    material_id: int = Field(gt=0)
    quantity: Quantity = Field(gt=0)
    movement_date: date | None = None
    counterparty_id: int | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, max_length=2000)


class StockIssueCreate(BaseModel):
    """P2-INV-001: stock OUT — general issue NOT tied to a production order."""

    model_config = ConfigDict(str_strip_whitespace=True)

    ip_id: int = Field(gt=0)
    material_id: int = Field(gt=0)
    quantity: Quantity = Field(gt=0)
    movement_date: date | None = None
    reason: str | None = Field(default=None, max_length=2000)


class StockAdjustmentCreate(BaseModel):
    """P2-INV-001: signed correction from a physical count (reason required)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    ip_id: int = Field(gt=0)
    material_id: int = Field(gt=0)
    quantity_delta: Quantity  # signed; direction carried by the sign
    movement_date: date | None = None
    reason: str = Field(min_length=1, max_length=2000)


class StockScrapCreate(BaseModel):
    """P2-INV-001: waste/offcuts — always negative (reason required)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    ip_id: int = Field(gt=0)
    material_id: int = Field(gt=0)
    quantity: Quantity = Field(gt=0)
    movement_date: date | None = None
    reason: str = Field(min_length=1, max_length=2000)


class ReservationCreate(BaseModel):
    """P2-INV-003: reserve stock for an order (intention, not a movement)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    order_id: int = Field(gt=0)
    material_id: int = Field(gt=0)
    quantity: Quantity = Field(gt=0)
    expires_at: datetime | None = None


class ReservationReleaseCreate(BaseModel):
    """P2-INV-003: release an ACTIVE reservation."""

    reason: Literal["ORDER_CANCELLED", "MANUAL", "EXPIRED", "SUPERSEDED"]


class ProductionOutputCreate(BaseModel):
    """G-C2: record a production-output event (quantity only, no valuation).

    order_id NULL = make-to-stock. When order_id is given, ip_id is derived
    from the order (DI-001 safe); otherwise ip_id is required.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    product_id: int = Field(gt=0)
    quantity: Quantity = Field(gt=0)
    order_id: int | None = Field(default=None, gt=0)
    ip_id: int | None = Field(default=None, gt=0)
    produced_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _ip_or_order(self) -> ProductionOutputCreate:
        if self.order_id is None and self.ip_id is None:
            raise ValueError("ip_id is required for make-to-stock output (no order_id)")
        return self
