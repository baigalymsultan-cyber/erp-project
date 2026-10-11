"""Accrual accounting: P&L by accrual_date, Cash Flow by operation_date (payment).

TDD for accrual_date feature (PHASE 4).

Scenario from user spec:
  January:  expense accrued = 500,000, payment = 0
  February: expense accrued = 0,       payment = 500,000

Expected:
  January P&L = -500,000 | January Cash Flow = 0
  February P&L effect = 0 | February Cash Flow = -500,000

And the mirror for revenue.
"""

import pytest
from sqlalchemy import text


@pytest.fixture
def accrual_data(engine, applied_schema):
    """Seed: one IP, expense accrued in Jan paid in Feb, revenue accrued in Jan paid in Feb."""
    import random

    bin_code = f"888{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('ACCRUAL-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()
        exp_type = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
        ).scalar_one()
        sale_type = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_SHOP'")
        ).scalar_one()

        # Expense: accrued 2026-01-15, paid 2026-02-10
        # Represented as: accrual operation (accrual_date=Jan, operation_date=Feb for payment leg?)
        # Model: single operation with accrual_date=2026-01-15, operation_date=2026-02-10
        # (payment date = when cash moved; accrual date = when expense recognized)
        conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, accrual_date, status) "
                "VALUES (:ip, :t, -500000.00, '2026-02-10', '2026-01-15', 'POSTED')"
            ),
            {"ip": ip_id, "t": exp_type},
        )
        # Revenue: accrued 2026-01-20, paid 2026-02-15
        conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, accrual_date, status) "
                "VALUES (:ip, :t, 800000.00, '2026-02-15', '2026-01-20', 'POSTED')"
            ),
            {"ip": ip_id, "t": sale_type},
        )
    return ip_id


def test_january_pnl_reflects_accrual(engine, applied_schema, accrual_data):
    """January P&L: expense -500k (accrued), revenue +800k (accrued)."""
    ip_id = accrual_data
    with engine.begin() as conn:
        # P&L should aggregate by accrual_date
        result = conn.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM operations "
                "WHERE ip_id = :ip AND status = 'POSTED' "
                "AND accrual_date >= '2026-01-01' "
                "AND accrual_date < '2026-02-01'"
            ),
            {"ip": ip_id},
        ).scalar_one()
    # -500000 + 800000 = 300000
    assert float(result) == 300000.00


def test_january_cash_flow_is_zero(engine, applied_schema, accrual_data):
    """January Cash Flow: no payments in January, so 0."""
    ip_id = accrual_data
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM operations "
                "WHERE ip_id = :ip AND status = 'POSTED' "
                "AND operation_date >= '2026-01-01' "
                "AND operation_date < '2026-02-01'"
            ),
            {"ip": ip_id},
        ).scalar_one()
    assert float(result) == 0.00


def test_february_pnl_effect_is_zero(engine, applied_schema, accrual_data):
    """February P&L: no new accruals, so 0 from these operations."""
    ip_id = accrual_data
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM operations "
                "WHERE ip_id = :ip AND status = 'POSTED' "
                "AND accrual_date >= '2026-02-01' "
                "AND accrual_date < '2026-03-01'"
            ),
            {"ip": ip_id},
        ).scalar_one()
    assert float(result) == 0.00


def test_february_cash_flow_reflects_payments(engine, applied_schema, accrual_data):
    """February Cash Flow: -500k (expense paid) + 800k (revenue received) = +300k."""
    ip_id = accrual_data
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM operations "
                "WHERE ip_id = :ip AND status = 'POSTED' "
                "AND operation_date >= '2026-02-01' "
                "AND operation_date < '2026-03-01'"
            ),
            {"ip": ip_id},
        ).scalar_one()
    assert float(result) == 300000.00


def test_accrual_date_nullable_for_pure_cash(engine, applied_schema):
    """Pure cash movements can have NULL accrual_date — never in P&L."""
    import random

    bin_code = f"777{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('ACCRUAL-NULL-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()
        # Use EXPENSE_MATERIAL with NULL accrual_date (pure cash, not yet accrued)
        # This verifies the column accepts NULL
        conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, accrual_date, status) "
                "SELECT :ip, id, -1000.00, '2026-01-10', NULL, 'POSTED' "
                "FROM operation_types WHERE code = 'EXPENSE_MATERIAL' LIMIT 1"
            ),
            {"ip": ip_id},
        )
        result = conn.execute(
            text(
                "SELECT accrual_date FROM operations WHERE ip_id = :ip"
            ),
            {"ip": ip_id},
        ).scalar_one()
    assert result is None
