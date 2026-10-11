"""Ledger + transfer schemas — §16 (append-only), §28, §63."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Money, OperationDirection

# --------------------------------------------------------------------------- #
# Operations (ledger) — §16: financial records are append-only
# --------------------------------------------------------------------------- #


class OperationCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    ip_id: int = Field(gt=0)
    type_id: int = Field(gt=0)
    category_id: int | None = Field(default=None, gt=0)
    direction: OperationDirection
    amount: Money = Field(frozen=False)
    operation_date: date
    # P&L recognition date. If not provided, defaults to operation_date
    # (cash basis). Set to None explicitly for pure payments/settlements.
    accrual_date: date | None = Field(default=None)
    comment: str | None = Field(default=None, max_length=500)
    external_order_id: str | None = Field(default=None, max_length=64)
    import_fingerprint: str | None = Field(default=None, max_length=64)


class OperationRead(OperationCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
# Transfers — §28: from_ip_id <> to_ip_id; §63: excluded from P&L
# --------------------------------------------------------------------------- #


class TransferCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    from_ip_id: int = Field(gt=0)
    to_ip_id: int = Field(gt=0)
    from_account_id: int = Field(gt=0)
    to_account_id: int = Field(gt=0)
    amount: Money = Field(gt=0)
    transfer_date: date
    comment: str | None = Field(default=None, max_length=500)


class TransferRead(TransferCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    outbound_operation_id: int
    inbound_operation_id: int
    created_by: int | None
    created_at: datetime
