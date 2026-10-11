"""Kaspi reconciliation services — §24, §44."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orders import Order
from app.schemas import ReconciliationRule


def reconcile_payout(
    session: Session,
    stored_gross: Decimal,
    stored_items_total: Decimal,
    rule: ReconciliationRule | None = None,
) -> bool:
    """§44: stored payout sums must match the item registry.

    Tolerances come from the reconciliation rule (OQ-004): the amount
    tolerance is in KZT, the date tolerance in days. Partial agreement is
    allowed only when the rule says so (allow_partial).
    """
    rule = rule or ReconciliationRule()
    diff = abs(Decimal(stored_gross) - Decimal(stored_items_total))
    if diff <= rule.amount_tolerance:
        return True
    return bool(rule.allow_partial) and diff <= rule.amount_tolerance * 2


def reconcile_payout_full(
    session: Session,
    payout_id: int,
    rule: ReconciliationRule | None = None,
) -> dict[str, object]:
    """§44 — full payout reconciliation across all 8 discrepancy classes.

    Every line of ``kaspi_payout_items`` for the payout is classified:

    * ``DUPLICATE`` — the order is already matched in another reconciled
      payout or twice within this payout;
    * ``MISSING_ORDER`` — the line references an external order that does
      not exist in ``orders`` for this ИП;
    * ``AMOUNT_MISMATCH`` — line gross ≠ ``orders.total_amount``;
    * ``COMMISSION_MISMATCH`` — line commission ≠ ``orders.kaspi_commission``;
    * ``DELIVERY_MISMATCH`` — line delivery ≠ ``orders.delivery_fee``;
    * ``PARTIAL`` — line net below the expected net and the rule allows
      partial agreement (OQ-004);
    * ``UNEXPLAINED`` — ADJUSTMENT/FEE line with no traceable order;
    * ``MATCHED`` — everything agrees within tolerance.

    The payout totals are cross-checked against the sum of its lines; the
    payout status becomes ``RECONCILED`` only when every line matched and
    the totals agree, otherwise ``DISCREPANCY``. ``reconciled_at`` is
    stamped either way — re-running the check is the documented way to
    clear a stale verdict (§16: no deletions).
    """
    from app.models.kaspi import KaspiPayout, KaspiPayoutItem

    rule = rule or ReconciliationRule()
    payout = session.get(KaspiPayout, payout_id)
    if payout is None:
        raise ValueError(f"payout {payout_id} not found")

    lines = session.scalars(
        select(KaspiPayoutItem)
        .where(KaspiPayoutItem.payout_id == payout_id)
        .order_by(KaspiPayoutItem.id)
    ).all()

    # Orders already matched by OTHER reconciled payouts — §44 duplicates.
    externally_matched_order_ids: set[int] = {
        int(order_id)
        for order_id in session.scalars(
            select(KaspiPayoutItem.order_id)
            .join(KaspiPayout, KaspiPayout.id == KaspiPayoutItem.payout_id)
            .where(
                KaspiPayoutItem.payout_id != payout_id,
                KaspiPayoutItem.order_id.is_not(None),
                KaspiPayout.status == "RECONCILED",
            )
        ).all()
        if order_id is not None
    }
    seen_in_payout: set[int] = set()

    verdicts: list[dict[str, object]] = []
    summary: dict[str, int] = {
        "MATCHED": 0,
        "AMOUNT_MISMATCH": 0,
        "COMMISSION_MISMATCH": 0,
        "DELIVERY_MISMATCH": 0,
        "DUPLICATE": 0,
        "MISSING_ORDER": 0,
        "PARTIAL": 0,
        "UNEXPLAINED": 0,
    }

    # §44 — a payout with no lines cannot explain where its money came
    # from: an empty report is a discrepancy, never a match.
    if not lines:
        payout.status = "DISCREPANCY"
        payout.reconciled_at = datetime.now(UTC)
        return {
            "payout_id": payout_id,
            "matched": False,
            "totals_match": False,
            "status": payout.status,
            "lines_total": 0,
            "summary": dict.fromkeys(summary, 0),
            "lines": [],
        }

    for line in lines:
        verdict = "MATCHED"
        detail = ""
        order = session.get(Order, line.order_id) if line.order_id is not None else None

        if order is None and line.external_order_id is not None:
            order = session.scalars(
                select(Order).where(
                    Order.ip_id == payout.ip_id,
                    Order.external_order_id == line.external_order_id,
                )
            ).first()

        if order is None and line.item_type not in ("ADJUSTMENT", "FEE"):
            verdict = "MISSING_ORDER"
            detail = f"order {line.external_order_id or line.order_id} not found"
        elif order is not None and line.item_type == "REFUND":
            # A refund line's gross is the refunded amount, not the order
            # total — traceability to the order is the only §44 check.
            seen_in_payout.add(order.id)
            detail = f"refund of {line.gross_amount} traced to order {order.id}"
        elif order is not None:
            if order.id in externally_matched_order_ids or order.id in seen_in_payout:
                verdict = "DUPLICATE"
                detail = f"order {order.id} already matched by another payout line"
            else:
                seen_in_payout.add(order.id)
                gross_diff = abs(Decimal(line.gross_amount) - Decimal(order.total_amount))
                commission_diff = abs(
                    Decimal(line.commission_amount) - Decimal(order.kaspi_commission or 0)
                )
                delivery_diff = abs(
                    Decimal(line.delivery_amount) - Decimal(order.delivery_fee or 0)
                )
                if gross_diff > rule.amount_tolerance:
                    verdict = "AMOUNT_MISMATCH"
                    detail = f"line gross {line.gross_amount} vs order {order.total_amount}"
                elif commission_diff > rule.amount_tolerance:
                    verdict = "COMMISSION_MISMATCH"
                    detail = (
                        f"line commission {line.commission_amount} "
                        f"vs order {order.kaspi_commission}"
                    )
                elif delivery_diff > rule.amount_tolerance:
                    verdict = "DELIVERY_MISMATCH"
                    detail = f"line delivery {line.delivery_amount} vs order {order.delivery_fee}"
                else:
                    expected_net = (
                        Decimal(order.total_amount)
                        - Decimal(order.kaspi_commission or 0)
                        - Decimal(order.delivery_fee or 0)
                    )
                    if Decimal(line.net_amount) < expected_net - rule.amount_tolerance:
                        if rule.allow_partial:
                            verdict = "PARTIAL"
                            detail = f"line net {line.net_amount} below expected {expected_net}"
                        else:
                            verdict = "AMOUNT_MISMATCH"
                            detail = f"line net {line.net_amount} below expected {expected_net}"
        else:
            # ADJUSTMENT/FEE without any order reference — untraceable money.
            verdict = "UNEXPLAINED"
            detail = f"{line.item_type} line without order reference"

        line.match_status = verdict
        summary[verdict] = summary.get(verdict, 0) + 1
        verdicts.append(
            {
                "item_id": line.id,
                "item_type": line.item_type,
                "external_order_id": line.external_order_id,
                "order_id": line.order_id,
                "verdict": verdict,
                "detail": detail,
            }
        )

    lines_gross = sum((Decimal(line.gross_amount) for line in lines), Decimal("0.00"))
    lines_net = sum((Decimal(line.net_amount) for line in lines), Decimal("0.00"))
    totals_match = (
        abs(lines_gross - Decimal(payout.gross_amount)) <= rule.amount_tolerance
        and abs(lines_net - Decimal(payout.net_amount)) <= rule.amount_tolerance
    )

    discrepancies = len(lines) - summary["MATCHED"]
    payout_status = "RECONCILED" if discrepancies == 0 and totals_match else "DISCREPANCY"
    payout.status = payout_status
    payout.reconciled_at = datetime.now(UTC)

    return {
        "payout_id": payout_id,
        "matched": discrepancies == 0 and totals_match,
        "totals_match": totals_match,
        "status": payout_status,
        "lines_total": len(lines),
        "summary": summary,
        "lines": verdicts,
    }
