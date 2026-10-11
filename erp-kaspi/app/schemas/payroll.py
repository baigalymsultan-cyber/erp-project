"""Payroll schemas — §32/§33 rate freeze; §34–36 manual input (OQ-007)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import Money, Quantity

# --------------------------------------------------------------------------- #
# Payroll — §32/§33: rate freeze; §34–36: manual input allowed (OQ-007)
# --------------------------------------------------------------------------- #


class WorkEntryCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    employee_id: int = Field(gt=0)
    work_operation_type_id: int = Field(gt=0)
    ip_id: int = Field(gt=0)
    # MASTER PROMPT 61 - the foreman flow is Employee -> Work Type -> Order ->
    # Quantity -> Date. The order link is optional: direct labour may be booked
    # without a customer order, but order costing (37/62) binds it when present.
    order_id: int | None = Field(default=None, gt=0)
    entry_date: date
    quantity: Quantity = Field(gt=0)
    comment: str | None = Field(default=None, max_length=500)
    # C-2: idempotency key for safe retries (uq_work_entries_import_fingerprint)
    import_fingerprint: str | None = Field(default=None, max_length=64)


class WorkEntryRead(WorkEntryCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    #: frozen at insert time from rates_history — §32
    rate_at_time: Money
    total_sum: Money


class PayrollPeriodCreate(BaseModel):
    period_start: date
    period_end: date
    comment: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _end_not_before_start(self) -> PayrollPeriodCreate:
        """API-side validation (§12): the DB CHECK would only surface as a
        low-level IntegrityError — the client deserves a real 422."""
        if self.period_end < self.period_start:
            raise ValueError("period_end must be >= period_start")
        return self


class PayrollAdjustment(BaseModel):
    """Manual bonuses / deductions for one employee in a period (§35, OQ-007)."""

    employee_id: int = Field(gt=0)
    bonuses: Money = Field(default=Decimal("0"))
    deductions: Money = Field(default=Decimal("0"))


class PayrollCalculateRequest(BaseModel):
    period_id: int = Field(gt=0)
    adjustments: list[PayrollAdjustment] = Field(default_factory=list)


class PayrollPayRequest(BaseModel):
    """§36 — a salary payment posts a SALARY_PAYMENT ledger operation."""

    summary_id: int = Field(gt=0)
    payment_date: date
    payment_method: Literal["KASPI_TRANSFER", "CASH"]
    comment: str | None = Field(default=None, max_length=500)
