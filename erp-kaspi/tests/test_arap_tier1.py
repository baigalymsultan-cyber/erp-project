"""AR/AP Tier 1: counterparty balances from operations (PHASE 5).

Formula: Balance = SUM(accruals) - SUM(cash settlements)
  Positive = AR (they owe us), Negative = AP (we owe them)
"""

import pytest
from sqlalchemy import text


@pytest.fixture
def arap_data(engine, applied_schema):
    import random

    bin_code = f"666{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('ARAP-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()
        # Create counterparties
        cust_id = conn.execute(
            text("INSERT INTO counterparties (name) VALUES ('Customer A') RETURNING id")
        ).scalar_one()
        supp_id = conn.execute(
            text("INSERT INTO counterparties (name) VALUES ('Supplier B') RETURNING id")
        ).scalar_one()

        sale_type = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_SHOP'")
        ).scalar_one()
        exp_type = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
        ).scalar_one()

        # Customer A: sale +800k accrued Jan 20, paid +800k Feb 15 → balance 0
        conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, counterparty_id, amount, "
                "operation_date, accrual_date, status) "
                "VALUES (:ip, :t, :cp, 800000.00, '2026-01-20', '2026-01-20', 'POSTED')"
            ),
            {"ip": ip_id, "t": sale_type, "cp": cust_id},
        )
        conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, counterparty_id, amount, "
                "operation_date, accrual_date, status) "
                "VALUES (:ip, :t, :cp, 800000.00, '2026-02-15', NULL, 'POSTED')"
            ),
            {"ip": ip_id, "t": sale_type, "cp": cust_id},
        )

        # Supplier B: expense -500k accrued Jan 15, NOT paid → balance -500k (AP)
        conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, counterparty_id, amount, "
                "operation_date, accrual_date, status) "
                "VALUES (:ip, :t, :cp, -500000.00, '2026-01-15', '2026-01-15', 'POSTED')"
            ),
            {"ip": ip_id, "t": exp_type, "cp": supp_id},
        )
    return ip_id, cust_id, supp_id


def test_settled_customer_balance_zero(engine, applied_schema, arap_data):
    """Customer paid in full → balance 0."""
    ip_id, cust_id, _ = arap_data
    with engine.begin() as conn:
        bal = conn.execute(
            text(
                "SELECT balance FROM v_counterparty_balances "
                "WHERE ip_id = :ip AND counterparty_id = :cp"
            ),
            {"ip": ip_id, "cp": cust_id},
        ).scalar_one()
    assert float(bal) == 0.00


def test_unpaid_supplier_balance_negative(engine, applied_schema, arap_data):
    """Supplier not paid → balance -500k (AP)."""
    ip_id, _, supp_id = arap_data
    with engine.begin() as conn:
        bal = conn.execute(
            text(
                "SELECT balance FROM v_counterparty_balances "
                "WHERE ip_id = :ip AND counterparty_id = :cp"
            ),
            {"ip": ip_id, "cp": supp_id},
        ).scalar_one()
    assert float(bal) == -500000.00
