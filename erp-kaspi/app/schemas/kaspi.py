"""Kaspi payloads — §42: raw JSONB, never invented fields (§94)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Money


class KaspiPayoutCreate(BaseModel):
    """§24/§42 — a payout registration payload. The endpoint used to accept
    ``OrderCreate`` here, which forced payout data through an order-shaped
    contract; that mismatch is fixed (audit #22)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    ip_id: int = Field(gt=0)
    external_payout_id: str = Field(min_length=1, max_length=64)
    payout_date: date
    gross_amount: Money = Field(gt=0)
    commission_amount: Money = Field(default=Decimal("0.00"), ge=0)
    status: Literal["EXPECTED", "RECEIVED"] = "EXPECTED"


# --------------------------------------------------------------------------- #
# Kaspi payloads — §42: raw JSONB, never invented fields (§94)
# --------------------------------------------------------------------------- #


class KaspiPayload(BaseModel):
    """Opaque envelope for raw Kaspi API payloads.

    OQ-003: the official Kaspi Shop API specification is not available
    (BLOCKED — EXTERNAL API SPECIFICATION REQUIRED). Until it is, payloads
    are stored and replayed as opaque JSON: fields are NOT interpreted.
    """

    api_type: Literal["SHOP", "PAY"]
    payload: dict[str, object]
    received_at: datetime | None = None


class ReconciliationRule(BaseModel):
    """Parameterised tolerance for payout reconciliation — OQ-004/§44."""

    amount_tolerance: Money = Field(default=Decimal("1.00"), ge=0)
    date_tolerance_days: int = Field(default=3, ge=0)
    allow_partial: bool = Field(default=True)
