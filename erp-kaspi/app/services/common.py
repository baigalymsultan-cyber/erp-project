"""Shared service utilities — money rounding, etc."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def _q2(value: Decimal) -> Decimal:
    """Money rounding — NUMERIC(15,2), half-up (§15)."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
