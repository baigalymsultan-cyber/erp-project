"""Pydantic v2 schemas — the API contract layer (MASTER PROMPT §12).

Money is Decimal(15,2) KZT (§15: currency is always KZT, OQ-014).
Quantities are Decimal(15,3) (OQ-012).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import Field

#: KZT money: NUMERIC(15,2) — §15
Money = Annotated[Decimal, Field(max_digits=15, decimal_places=2)]
#: Production quantities: NUMERIC(15,3) — OQ-012 (м², пог. м)
Quantity = Annotated[Decimal, Field(max_digits=15, decimal_places=3)]


class OperationDirection(StrEnum):
    """§27 — matches operation_types.direction in the schema ('IN' | 'OUT')."""

    IN = "IN"
    OUT = "OUT"


class OrderStatus(StrEnum):
    """The state machine of the database (chk_orders_status) — single source
    of truth is ``app.models.orders.ORDER_STATUSES``. The API used to ship a
    different, incompatible set of values (IN_PRODUCTION/READY/SHIPPED) that
    PostgreSQL would have rejected."""

    NEW = "NEW"
    APPROVED = "APPROVED"
    ASSEMBLING = "ASSEMBLING"
    DELIVERING = "DELIVERING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    RETURNED = "RETURNED"


class PaymentMethod(StrEnum):
    KASPI_PAY = "KASPI_PAY"
    KASPI_MAGAZIN = "KASPI_MAGAZIN"
    BANK_TRANSFER = "BANK_TRANSFER"
    CASH = "CASH"


class EmployeePaymentType(StrEnum):
    SALARY = "SALARY"
    PIECEWORK = "PIECEWORK"
    PIECEWORK_BONUS = "PIECEWORK_BONUS"
