"""Full payout reconciliation — §44, all 8 discrepancy classes (REQ-0307).

Every test runs inside a rollback transaction (§68, TD-012); no business
data survives the run (§94).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.kaspi import KaspiPayout, KaspiPayoutItem
from app.models.orders import Order
from app.schemas import ReconciliationRule
from app.services import reconcile_payout_full

TIGHT = ReconciliationRule(amount_tolerance=Decimal("1.00"), allow_partial=True)
NO_PARTIAL = ReconciliationRule(amount_tolerance=Decimal("1.00"), allow_partial=False)


@pytest.fixture
def ip_id(session: Session) -> int:
    from sqlalchemy import text

    return session.execute(
        text(
            "INSERT INTO ip_entities (name, bin) VALUES ('RECON QA ИП', '677777777777') "
            "ON CONFLICT (bin) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        )
    ).scalar_one()


def _order(session: Session, ip_id: int, external_id: str | None, total: str,
           commission: str = "0", delivery: str = "0") -> Order:
    order = Order(
        ip_id=ip_id,
        external_order_id=external_id,
        status="NEW",
        total_amount=Decimal(total),
        kaspi_commission=Decimal(commission),
        delivery_fee=Decimal(delivery),
    )
    session.add(order)
    session.flush()
    return order


_PAYOUT_SEQ = 0


def _payout(session: Session, ip_id: int, gross: str, net: str) -> KaspiPayout:
    global _PAYOUT_SEQ
    _PAYOUT_SEQ += 1
    payout = KaspiPayout(
        ip_id=ip_id,
        external_payout_id=f"pay-{_PAYOUT_SEQ}",
        payout_date=date(2026, 9, 1),
        gross_amount=Decimal(gross),
        net_amount=Decimal(net),
        status="RECEIVED",
    )
    session.add(payout)
    session.flush()
    return payout


def _line(session: Session, payout: KaspiPayout, *, order_id: int | None = None,
          external_id: str | None = None, gross: str, commission: str = "0",
          delivery: str = "0", net: str | None = None,
          item_type: str = "SALE") -> KaspiPayoutItem:
    line = KaspiPayoutItem(
        payout_id=payout.id,
        order_id=order_id,
        external_order_id=external_id,
        gross_amount=Decimal(gross),
        commission_amount=Decimal(commission),
        delivery_amount=Decimal(delivery),
        refund_amount=Decimal("0"),
        net_amount=Decimal(net) if net is not None else Decimal(gross)
        - Decimal(commission)
        - Decimal(delivery),
        item_type=item_type,
    )
    session.add(line)
    session.flush()
    return line


def test_matched_payout_is_reconciled(session, ip_id):
    order = _order(session, ip_id, "ext-1", "10000", "500", "300")
    payout = _payout(session, ip_id, "10000", "9200")
    _line(session, payout, order_id=order.id, gross="10000", commission="500",
          delivery="300", net="9200")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["matched"] is True
    assert report["status"] == "RECONCILED"
    assert report["totals_match"] is True
    assert report["summary"]["MATCHED"] == 1
    assert payout.status == "RECONCILED"
    assert payout.reconciled_at is not None


def test_amount_mismatch_detected(session, ip_id):
    order = _order(session, ip_id, "ext-2", "10000")
    payout = _payout(session, ip_id, "12000", "12000")
    _line(session, payout, order_id=order.id, gross="12000", net="12000")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["matched"] is False
    assert report["summary"]["AMOUNT_MISMATCH"] == 1
    assert report["lines"][0]["verdict"] == "AMOUNT_MISMATCH"


def test_commission_mismatch_detected(session, ip_id):
    order = _order(session, ip_id, "ext-3", "10000", commission="500")
    payout = _payout(session, ip_id, "10000", "9000")
    _line(session, payout, order_id=order.id, gross="10000", commission="1500",
          net="8500")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["summary"]["COMMISSION_MISMATCH"] == 1
    assert report["status"] == "DISCREPANCY"


def test_delivery_mismatch_detected(session, ip_id):
    order = _order(session, ip_id, "ext-4", "10000", delivery="300")
    payout = _payout(session, ip_id, "10000", "9400")
    _line(session, payout, order_id=order.id, gross="10000", delivery="900",
          net="9100")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["summary"]["DELIVERY_MISMATCH"] == 1


def test_missing_order_detected(session, ip_id):
    payout = _payout(session, ip_id, "5000", "5000")
    _line(session, payout, external_id="ghost-order", gross="5000", net="5000")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["summary"]["MISSING_ORDER"] == 1
    assert report["lines"][0]["verdict"] == "MISSING_ORDER"


def test_duplicate_order_within_payout(session, ip_id):
    order = _order(session, ip_id, "ext-5", "4000")
    payout = _payout(session, ip_id, "8000", "8000")
    _line(session, payout, order_id=order.id, gross="4000", net="4000")
    # uq_kaspi_payout_items_order blocks a second (payout, order, SALE)
    # row, so a real-world duplicate arrives unresolvable: same external
    # id, no order_id. The service must trace it back to the order.
    _line(session, payout, external_id="ext-5", gross="4000", net="4000")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["summary"]["DUPLICATE"] == 1
    assert report["lines"][0]["verdict"] == "MATCHED"
    assert report["lines"][1]["verdict"] == "DUPLICATE"


def test_duplicate_across_reconciled_payouts(session, ip_id):
    order = _order(session, ip_id, "ext-6", "4000")
    first = _payout(session, ip_id, "4000", "4000")
    _line(session, first, order_id=order.id, gross="4000", net="4000")
    assert reconcile_payout_full(session, first.id, TIGHT)["matched"] is True

    second = _payout(session, ip_id, "4000", "4000")
    _line(session, second, order_id=order.id, gross="4000", net="4000")
    report = reconcile_payout_full(session, second.id, TIGHT)
    assert report["summary"]["DUPLICATE"] == 1


def test_partial_payout_allowed_when_rule_permits(session, ip_id):
    order = _order(session, ip_id, "ext-7", "10000")
    payout = _payout(session, ip_id, "10000", "5000")
    _line(session, payout, order_id=order.id, gross="10000", net="5000")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["summary"]["PARTIAL"] == 1

    strict = reconcile_payout_full(session, payout.id, NO_PARTIAL)
    assert strict["summary"]["AMOUNT_MISMATCH"] == 1


def test_unexplained_adjustment_line(session, ip_id):
    payout = _payout(session, ip_id, "100", "100")
    _line(session, payout, gross="100", net="100", item_type="ADJUSTMENT")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["summary"]["UNEXPLAINED"] == 1


def test_totals_mismatch_marks_discrepancy(session, ip_id):
    order = _order(session, ip_id, "ext-8", "10000")
    payout = _payout(session, ip_id, "15000", "15000")
    _line(session, payout, order_id=order.id, gross="10000", net="10000")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["totals_match"] is False
    assert report["matched"] is False
    assert report["status"] == "DISCREPANCY"


def test_refund_line_matched_against_order(session, ip_id):
    order = _order(session, ip_id, "ext-9", "10000")
    payout = _payout(session, ip_id, "3000", "3000")
    _line(session, payout, order_id=order.id, gross="3000", net="3000",
          item_type="REFUND")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["summary"]["MATCHED"] == 1
    assert report["status"] == "RECONCILED"


def test_unknown_payout_raises(session):
    with pytest.raises(ValueError, match="not found"):
        reconcile_payout_full(session, 10**9, TIGHT)


def test_empty_payout_is_discrepancy(session, ip_id):
    payout = _payout(session, ip_id, "0", "0")
    report = reconcile_payout_full(session, payout.id, TIGHT)
    assert report["lines_total"] == 0
    assert report["matched"] is False
    assert report["status"] == "DISCREPANCY"
