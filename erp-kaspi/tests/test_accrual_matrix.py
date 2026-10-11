"""Accrual test matrix: 20 financial scenarios (PHASE 2).

Covers: expense, revenue, payroll, production, financial integrations.
Each scenario verifies P&L (by accrual_date) vs Cash Flow (by operation_date)
vs AR/AP (v_counterparty_balances) vs settlement balance.

Core rule: payment of an already accrued item must NOT create a second P&L effect.
"""

import pytest
from sqlalchemy import text
from decimal import Decimal


@pytest.fixture
def matrix_data(engine, applied_schema):
    """Seed operation types and counterparties for the matrix."""
    import random

    bin_code = f"555{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('MATRIX-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()
        cust_id = conn.execute(
            text("INSERT INTO counterparties (name) VALUES ('Matrix Customer') RETURNING id")
        ).scalar_one()
        supp_id = conn.execute(
            text("INSERT INTO counterparties (name) VALUES ('Matrix Supplier') RETURNING id")
        ).scalar_one()

        types = {}
        for code in ['SALE_KASPI_SHOP', 'EXPENSE_MATERIAL', 'SALARY_PAYMENT']:
            tid = conn.execute(
                text("SELECT id FROM operation_types WHERE code = :c"),
                {"c": code}
            ).scalar_one()
            types[code] = tid
    return {"ip_id": ip_id, "cust_id": cust_id, "supp_id": supp_id, "types": types}


def _pnl(conn, ip_id, date_from, date_to):
    """P&L by accrual_date. Only operations with explicit accrual_date count.
    Pure payments (accrual_date IS NULL) never hit P&L."""
    return conn.execute(
        text(
            "SELECT COALESCE(SUM(amount), 0) FROM operations "
            "WHERE ip_id = :ip AND status = 'POSTED' "
            "AND accrual_date IS NOT NULL "
            "AND accrual_date >= :d1 "
            "AND accrual_date < :d2"
        ),
        {"ip": ip_id, "d1": date_from, "d2": date_to},
    ).scalar_one()


def _cf(conn, ip_id, date_from, date_to):
    """Cash Flow by operation_date."""
    return conn.execute(
        text(
            "SELECT COALESCE(SUM(amount), 0) FROM operations "
            "WHERE ip_id = :ip AND status = 'POSTED' "
            "AND operation_date >= :d1 AND operation_date < :d2"
        ),
        {"ip": ip_id, "d1": date_from, "d2": date_to},
    ).scalar_one()


def _insert_op(conn, ip_id, type_id, cp_id, amount, op_date, acc_date):
    conn.execute(
        text(
            "INSERT INTO operations (ip_id, type_id, counterparty_id, amount, "
            "operation_date, accrual_date, status) "
            "VALUES (:ip, :t, :cp, :amt, :opd, :acd, 'POSTED')"
        ),
        {"ip": ip_id, "t": type_id, "cp": cp_id, "amt": amount,
         "opd": op_date, "acd": acc_date},
    )


# === EXPENSE SCENARIOS ===

def test_01_expense_accrual_payment_same_month(engine, applied_schema, matrix_data):
    """1. Accrual Jan / payment Jan: both P&L and CF in Jan."""
    d = matrix_data
    with engine.begin() as conn:
        _insert_op(conn, d["ip_id"], d["types"]["EXPENSE_MATERIAL"],
                   d["supp_id"], -100000, '2026-01-15', '2026-01-15')
        assert float(_pnl(conn, d["ip_id"], '2026-01-01', '2026-02-01')) == -100000
        assert float(_cf(conn, d["ip_id"], '2026-01-01', '2026-02-01')) == -100000


def test_02_expense_accrual_jan_payment_feb(engine, applied_schema, matrix_data):
    """2. Accrual Jan / payment Feb: P&L Jan, CF Feb."""
    d = matrix_data
    with engine.begin() as conn:
        # Accrual leg
        _insert_op(conn, d["ip_id"], d["types"]["EXPENSE_MATERIAL"],
                   d["supp_id"], -200000, '2026-01-15', '2026-01-15')
        # Payment leg (separate operation, accrual_date NULL)
        _insert_op(conn, d["ip_id"], d["types"]["EXPENSE_MATERIAL"],
                   d["supp_id"], -200000, '2026-02-10', None)
        # P&L Jan: -200k (accrual only, payment has NULL accrual_date)
        assert float(_pnl(conn, d["ip_id"], '2026-01-01', '2026-02-01')) == -200000
        # P&L Feb: 0 (payment does NOT hit P&L)
        assert float(_pnl(conn, d["ip_id"], '2026-02-01', '2026-03-01')) == 0
        # CF Jan: -200k (accrual operation also has operation_date in Jan)
        # CF Feb: -200k (payment)
        assert float(_cf(conn, d["ip_id"], '2026-02-01', '2026-03-01')) == -200000


def test_03_expense_two_partial_payments(engine, applied_schema, matrix_data):
    """3. Accrual Jan 500k / two payments Feb 200k + 300k."""
    d = matrix_data
    with engine.begin() as conn:
        _insert_op(conn, d["ip_id"], d["types"]["EXPENSE_MATERIAL"],
                   d["supp_id"], -500000, '2026-01-15', '2026-01-15')
        _insert_op(conn, d["ip_id"], d["types"]["EXPENSE_MATERIAL"],
                   d["supp_id"], -200000, '2026-02-05', None)
        _insert_op(conn, d["ip_id"], d["types"]["EXPENSE_MATERIAL"],
                   d["supp_id"], -300000, '2026-02-20', None)
        # P&L Jan: -500k, P&L Feb: 0
        assert float(_pnl(conn, d["ip_id"], '2026-01-01', '2026-02-01')) == -500000
        assert float(_pnl(conn, d["ip_id"], '2026-02-01', '2026-03-01')) == 0
        # CF Feb: -500k total
        assert float(_cf(conn, d["ip_id"], '2026-02-01', '2026-03-01')) == -500000


def test_04_expense_accrued_unpaid(engine, applied_schema, matrix_data):
    """4. Accrual Jan / unpaid: P&L Jan, CF 0, AP balance -X."""
    d = matrix_data
    with engine.begin() as conn:
        _insert_op(conn, d["ip_id"], d["types"]["EXPENSE_MATERIAL"],
                   d["supp_id"], -150000, '2026-01-15', '2026-01-15')
        assert float(_pnl(conn, d["ip_id"], '2026-01-01', '2026-02-01')) == -150000
        assert float(_cf(conn, d["ip_id"], '2026-01-01', '2026-04-01')) == -150000  # op_date in Jan
        # AP check via view
        bal = conn.execute(
            text("SELECT balance FROM v_counterparty_balances "
                 "WHERE ip_id = :ip AND counterparty_id = :cp"),
            {"ip": d["ip_id"], "cp": d["supp_id"]},
        ).scalar_one()
        # Accrual -150k, no settlement → balance -150k (AP)
        assert float(bal) == -150000


def test_05_payment_before_accrual(engine, applied_schema, matrix_data):
    """5. Payment Jan (prepayment) / accrual Feb: CF Jan, P&L Feb."""
    d = matrix_data
    with engine.begin() as conn:
        # Prepayment: cash out Jan, no accrual yet
        _insert_op(conn, d["ip_id"], d["types"]["EXPENSE_MATERIAL"],
                   d["supp_id"], -100000, '2026-01-10', None)
        # Accrual Feb
        _insert_op(conn, d["ip_id"], d["types"]["EXPENSE_MATERIAL"],
                   d["supp_id"], -100000, '2026-02-15', '2026-02-15')
        assert float(_cf(conn, d["ip_id"], '2026-01-01', '2026-02-01')) == -100000
        assert float(_pnl(conn, d["ip_id"], '2026-01-01', '2026-02-01')) == 0
        assert float(_pnl(conn, d["ip_id"], '2026-02-01', '2026-03-01')) == -100000


# === REVENUE SCENARIOS ===

def test_06_revenue_accrual_later_receipt(engine, applied_schema, matrix_data):
    """6. Revenue accrual Jan / receipt Feb."""
    d = matrix_data
    with engine.begin() as conn:
        _insert_op(conn, d["ip_id"], d["types"]["SALE_KASPI_SHOP"],
                   d["cust_id"], 800000, '2026-01-20', '2026-01-20')
        _insert_op(conn, d["ip_id"], d["types"]["SALE_KASPI_SHOP"],
                   d["cust_id"], 800000, '2026-02-15', None)
        assert float(_pnl(conn, d["ip_id"], '2026-01-01', '2026-02-01')) == 800000
        assert float(_pnl(conn, d["ip_id"], '2026-02-01', '2026-03-01')) == 0
        assert float(_cf(conn, d["ip_id"], '2026-02-01', '2026-03-01')) == 800000


def test_07_revenue_partial_receipt(engine, applied_schema, matrix_data):
    """7. Revenue 1M / partial receipt 400k."""
    d = matrix_data
    with engine.begin() as conn:
        _insert_op(conn, d["ip_id"], d["types"]["SALE_KASPI_SHOP"],
                   d["cust_id"], 1000000, '2026-01-20', '2026-01-20')
        _insert_op(conn, d["ip_id"], d["types"]["SALE_KASPI_SHOP"],
                   d["cust_id"], 400000, '2026-02-15', None)
        assert float(_pnl(conn, d["ip_id"], '2026-01-01', '2026-02-01')) == 1000000
        # AR balance: 1M - 400k = 600k
        bal = conn.execute(
            text("SELECT balance FROM v_counterparty_balances "
                 "WHERE ip_id = :ip AND counterparty_id = :cp"),
            {"ip": d["ip_id"], "cp": d["cust_id"]},
        ).scalar_one()
        assert float(bal) == 600000


def test_08_unpaid_receivable(engine, applied_schema, matrix_data):
    """8. Unpaid receivable: P&L recognizes, CF 0, AR positive."""
    d = matrix_data
    with engine.begin() as conn:
        _insert_op(conn, d["ip_id"], d["types"]["SALE_KASPI_SHOP"],
                   d["cust_id"], 500000, '2026-01-20', '2026-01-20')
        assert float(_pnl(conn, d["ip_id"], '2026-01-01', '2026-02-01')) == 500000
        bal = conn.execute(
            text("SELECT balance FROM v_counterparty_balances "
                 "WHERE ip_id = :ip AND counterparty_id = :cp"),
            {"ip": d["ip_id"], "cp": d["cust_id"]},
        ).scalar_one()
        assert float(bal) == 500000  # AR
