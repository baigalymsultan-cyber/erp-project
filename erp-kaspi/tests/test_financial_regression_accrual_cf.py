"""Financial regression: accrual_date vs operation_date semantics (2026-10-04).

User-required scenario (verbatim):
    January 2026:  expense accrued = 500,000, payment = 0
    February 2026: expense accrued = 0,       payment = 500,000

Expected:
    P&L January  = -500,000 | Cash Flow January  = 0
    P&L February effect = 0 | Cash Flow February = -500,000

Unlike tests/test_accrual_accounting.py and tests/test_accrual_matrix.py
(which mirror the logic with raw-SQL helpers), this test exercises the REAL
code paths under verification:
  * app.services.add_operation()  — accrual_date passthrough (not defaulted)
  * app.services.pnl()            — accrual_date filter (not operation_date)
  * DB function cash_flow()       — operation_date filter (not accrual_date)

Modeling convention (documents test_accrual_accounting.py fixture):
  single ledger row with accrual_date = 2026-01-15 (economic event) and
  operation_date = 2026-02-10 (cash movement). operation_date is the cash
  date; accrual_date is the P&L recognition date.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.schemas import OperationCreate, OperationDirection, PayrollPeriodCreate
from app.services import add_operation, pnl


@pytest.fixture
def accrual_ip(session, applied_schema):
    """Fresh ИП with no other operations (start balance = 0)."""
    import random

    bin_code = f"996{random.randint(100000000, 999999999)}"
    ip_id = session.execute(
        text("INSERT INTO ip_entities (name, bin) VALUES ('ACCRUAL-REG-TEST', :b) RETURNING id"),
        {"b": bin_code},
    ).scalar_one()
    exp_type = session.execute(
        text("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
    ).scalar_one()
    session.flush()
    return {"ip_id": ip_id, "exp_type": exp_type}


def _cash_flow(session, ip_id, d1, d2):
    """Call the real DB cash_flow() function (uses operation_date)."""
    return (
        session.execute(
            text("SELECT * FROM cash_flow(:ip, :df, :dt)"),
            {"ip": ip_id, "df": d1, "dt": d2},
        )
        .mappings()
        .one()
    )


def test_accrual_jan_paid_feb_service_level(session, applied_schema, accrual_ip):
    """Expense accrued Jan 2026 (500k), paid Feb 2026 — end-to-end semantics.

    P&L Jan = -500,000 | CF Jan = 0 | CF Feb = -500,000 | P&L Feb effect = 0.
    """
    d = accrual_ip

    # The write path: add_operation must store accrual_date as given,
    # NOT silently replace it with operation_date.
    op_id = add_operation(
        session,
        OperationCreate(
            ip_id=d["ip_id"],
            type_id=d["exp_type"],
            direction=OperationDirection.OUT,
            amount=Decimal("-500000"),
            operation_date=date(2026, 2, 10),  # cash moved in February
            accrual_date=date(2026, 1, 15),  # expense recognized in January
            comment="financial regression 2026-10-04",
        ),
    )
    session.flush()
    stored = session.execute(
        text("SELECT accrual_date, operation_date FROM operations WHERE id = :i"),
        {"i": op_id},
    ).one()
    assert stored.accrual_date == date(2026, 1, 15)
    assert stored.operation_date == date(2026, 2, 10)

    jan = PayrollPeriodCreate(period_start=date(2026, 1, 1), period_end=date(2026, 1, 31))
    feb = PayrollPeriodCreate(period_start=date(2026, 2, 1), period_end=date(2026, 2, 28))

    # --- P&L (service): aggregates by accrual_date ---
    pnl_jan = pnl(session, period=jan, ip_ids=[d["ip_id"]])
    assert pnl_jan["total_income"] == Decimal("0.00")
    assert pnl_jan["total_expense"] == Decimal("500000.00")
    assert pnl_jan["net_profit"] == Decimal("-500000.00")

    # Payment in February must NOT create a second P&L effect.
    pnl_feb = pnl(session, period=feb, ip_ids=[d["ip_id"]])
    assert pnl_feb["total_income"] == Decimal("0.00")
    assert pnl_feb["total_expense"] == Decimal("0.00")
    assert pnl_feb["net_profit"] == Decimal("0.00")

    # --- Cash Flow (DB function): aggregates by operation_date ---
    cf_jan = _cash_flow(session, d["ip_id"], date(2026, 1, 1), date(2026, 1, 31))
    assert cf_jan["materials"] == Decimal("0.00")
    assert cf_jan["ending_balance"] == Decimal("0.00")

    # EXPENSE_MATERIAL is reported on the cash_flow() "materials" line
    # (positive magnitude); ending_balance carries the signed outflow.
    cf_feb = _cash_flow(session, d["ip_id"], date(2026, 2, 1), date(2026, 2, 28))
    assert cf_feb["materials"] == Decimal("500000.00")
    assert cf_feb["ending_balance"] == Decimal("-500000.00")
