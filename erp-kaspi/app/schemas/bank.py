"""Bank statement schemas (G-BANK-2..5)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Bank statements (G-BANK-2..5)
# --------------------------------------------------------------------------- #


class PostingPolicySchema(BaseModel):
    """Explicit human-approved policy for post_unmatched_as_operations."""

    model_config = ConfigDict(str_strip_whitespace=True)

    default_status: Literal["DRAFT", "POSTED"] = "DRAFT"
    accrual_date: date | None = None
    commission_knp_codes: list[str] = Field(default_factory=list, max_length=50)
    amount_tolerance: Decimal = Field(default=Decimal("1.00"), ge=0, le=1000)
    date_window_days: int = Field(default=1, ge=0, le=30)


class BankLinesPostRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    line_ids: list[int] = Field(min_length=1, max_length=500)
    policy: PostingPolicySchema = Field(default_factory=PostingPolicySchema)


class BankLineConfirmRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    operation_id: int = Field(gt=0)


class BankLineIgnoreRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=10, max_length=2000)


class BankLinesSuggestRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    statement_id: int | None = Field(default=None, gt=0)
    account_id: int | None = Field(default=None, gt=0)
