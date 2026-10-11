"""Payroll IP scoping: payroll_period_totals must not leak across ИПs.

``payroll_period_totals`` aggregates work-entry accruals for a payroll
period. When ``ip_ids`` is supplied the totals must cover only those ИПs;
this guards the latent cross-IP leak (the function previously summed all
ИПs unconditionally).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.schemas import PayrollPeriodCreate
from app.services import payroll_period_totals


@pytest.fixture()
def _two_ip_work(session):
    """Two ИПs with confirmed work entries in the same period."""
    s = session
    ip1 = s.execute(
        text("INSERT INTO ip_entities (name, bin) VALUES ('IP1','900000000101') RETURNING id")
    ).scalar_one()
    ip2 = s.execute(
        text("INSERT INTO ip_entities (name, bin) VALUES ('IP2','900000000102') RETURNING id")
    ).scalar_one()
    e1 = s.execute(
        text(
            "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
            "VALUES ('E1', :ip, 'PIECEWORK') RETURNING id"
        ),
        {"ip": ip1},
    ).scalar_one()
    e2 = s.execute(
        text(
            "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
            "VALUES ('E2', :ip, 'PIECEWORK') RETURNING id"
        ),
        {"ip": ip2},
    ).scalar_one()
    wot = s.execute(
        text(
            "INSERT INTO work_operation_types (code, name, unit, calc_mode) "
            "VALUES ('WOT-TEST', 'WOT', 'шт', 'per_unit') RETURNING id"
        )
    ).scalar_one()
    for emp, ip, qty, rate in ((e1, ip1, 1, "1000.00"), (e2, ip2, 1, "2500.00")):
        s.execute(
            text(
                "INSERT INTO work_entries (employee_id, work_operation_type_id, ip_id, "
                "entry_date, status, quantity, rate_at_time) "
                "VALUES (:emp, :wot, :ip, :d, 'CONFIRMED', :q, :r)"
            ),
            {"emp": emp, "wot": wot, "ip": ip, "d": date(2026, 9, 15), "q": qty, "r": rate},
        )
    s.commit()
    return ip1, ip2


def _period() -> PayrollPeriodCreate:
    return PayrollPeriodCreate(period_start=date(2026, 9, 1), period_end=date(2026, 9, 30))


def test_payroll_totals_scoped_to_given_ips(session, _two_ip_work):
    ip1, _ip2 = _two_ip_work
    got = payroll_period_totals(session, _period(), ip_ids=[ip1])
    assert got["accrued"] == Decimal("1000.00")
    assert got["to_pay"] == Decimal("1000.00")


def test_payroll_totals_both_ips_when_both_listed(session, _two_ip_work):
    ip1, ip2 = _two_ip_work
    got = payroll_period_totals(session, _period(), ip_ids=[ip1, ip2])
    assert got["accrued"] == Decimal("3500.00")


def test_payroll_totals_empty_ip_list_counts_nothing(session, _two_ip_work):
    got = payroll_period_totals(session, _period(), ip_ids=[])
    assert got["accrued"] == Decimal("0.00")
