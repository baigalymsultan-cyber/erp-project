"""ШАГ 7 — §34–36 payroll flow: calculate → approve → pay, with the §57
double-payment guard. Covers REQ-0309 (payroll summaries/payments → ledger)
and the row-lock part of REQ-0312 (SELECT FOR UPDATE / unique constraints).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api import app
from app.services import (
    approve_payroll_summary,
    calculate_payroll,
    pay_payroll_summary,
)


def _seed_payroll(engine: Engine) -> dict[str, int]:
    """ИП + two employees + work type + rate + CONFIRMED/DRAFT entries."""
    run = uuid.uuid4().hex[:10]
    with engine.begin() as conn:
        ip_id = conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) VALUES (:n, :b) "
                "ON CONFLICT (bin) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            ),
            {"n": f"PAYROLL ИП {run}", "b": f"9{str(int(run, 16) % 10**11).zfill(11)}"},
        ).scalar_one()
        piecework_emp = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
                "VALUES (:n, :ip, 'PIECEWORK') RETURNING id"
            ),
            {"n": f"Сдельщик {run}", "ip": ip_id},
        ).scalar_one()
        salaried_emp = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary) "
                "VALUES (:n, :ip, 'SALARY', 200000.00) RETURNING id"
            ),
            {"n": f"Окладник {run}", "ip": ip_id},
        ).scalar_one()
        wot_id = conn.execute(
            text(
                "INSERT INTO work_operation_types (code, name, unit, calc_mode) "
                "VALUES (:c, :n, 'pcs', 'per_unit') ON CONFLICT (code) DO NOTHING RETURNING id"
            ),
            {"c": f"PAYROLL_{run}", "n": f"Операция {run}"},
        ).scalar_one_or_none()
        if wot_id is None:
            wot_id = conn.execute(
                text("SELECT id FROM work_operation_types WHERE code = :c"),
                {"c": f"PAYROLL_{run}"},
            ).scalar_one()
        off = int(run, 16) % 600
        p_start = date(2010 + off // 12, 1 + off % 12, 1)
        while conn.execute(
            text("SELECT 1 FROM payroll_periods WHERE start_date = :start_date"),
            {"start_date": p_start},
        ).scalar_one_or_none():
            off = (off + 1) % 600
            p_start = date(2010 + off // 12, 1 + off % 12, 1)
        conn.execute(
            text(
                "INSERT INTO rates_history "
                "(work_operation_type_id, rate, effective_from, effective_to) "
                "VALUES (:w, 250.00, :from_ts, :to_ts)"
            ),
            {
                "w": wot_id,
                "from_ts": p_start,
                "to_ts": p_start.replace(day=28),
            },
        )
        for qty, day, entry_status in (
            (4, 10, "CONFIRMED"),
            (2, 20, "CONFIRMED"),
            (10, 25, "DRAFT"),
        ):
            conn.execute(
                text(
                    "INSERT INTO work_entries (employee_id, work_operation_type_id, ip_id, "
                    "entry_date, quantity, status) "
                    "VALUES (:e, :w, :ip, CAST(:d AS DATE), :q, :s)"
                ),
                {
                    "e": piecework_emp,
                    "w": wot_id,
                    "ip": ip_id,
                    "d": f"{p_start.year}-{p_start.month:02d}-{day:02d}",
                    "q": qty,
                    "s": entry_status,
                },
            )
        period_id = conn.execute(
            text(
                "INSERT INTO payroll_periods (start_date, end_date) "
                "SELECT make_date(:y, :m, 1), "
                "(make_date(:y, :m, 1) + INTERVAL '1 month' - INTERVAL '1 day')::date "
                "RETURNING id"
            ),
            {"y": p_start.year, "m": p_start.month},
        ).scalar_one()
    return {
        "ip_id": ip_id,
        "piecework_emp": piecework_emp,
        "salaried_emp": salaried_emp,
        "period_id": period_id,
    }


def test_calculate_payroll_aggregates_piecework_and_base(session: Session, engine: Engine):
    """§35 — CONFIRMED entries aggregate; DRAFT excluded; SALARY base applies;
    recalculation is idempotent (§57 unique employee+period)."""
    ids = _seed_payroll(engine)

    first = calculate_payroll(session, ids["period_id"])
    session.commit()

    by_emp = {row["employee_id"]: row for row in first}
    assert Decimal(by_emp[ids["piecework_emp"]]["piecework_total"]) == Decimal("1500.00"), (
        "4*250 + 2*250; the DRAFT entry (10*250) must be excluded"
    )
    assert Decimal(by_emp[ids["salaried_emp"]]["piecework_total"]) == Decimal("0.00")
    assert Decimal(by_emp[ids["salaried_emp"]]["base_salary"]) == Decimal("200000.00")
    assert by_emp[ids["piecework_emp"]]["total_calculated"] == "1500.00"
    assert by_emp[ids["salaried_emp"]]["total_calculated"] == "200000.00"
    assert all(row["status"] == "CALCULATED" for row in first)

    second = calculate_payroll(session, ids["period_id"])
    session.commit()
    assert sorted(r["id"] for r in second) == sorted(r["id"] for r in first), (
        "recalculation updates the same rows — never duplicates (§57)"
    )

    period_status = session.scalar(
        text("SELECT status FROM payroll_periods WHERE id = :p"), {"p": ids["period_id"]}
    )
    assert period_status == "PROCESSING", "§34: a calculated period moves to PROCESSING"


def test_pay_flow_posts_salary_operation(session: Session, engine: Engine):
    """§36 — pay posts ONE SALARY_PAYMENT ledger operation, one payroll_payment,
    freezes the work entries as PAID, closes the period (§34)."""
    ids = _seed_payroll(engine)
    summaries = calculate_payroll(session, ids["period_id"])
    session.commit()
    target = next(r for r in summaries if r["employee_id"] == ids["piecework_emp"])

    approve_payroll_summary(session, int(target["id"]))
    paid = pay_payroll_summary(
        session,
        int(target["id"]),
        date(2026, 4, 5),
        "CASH",
    )
    session.commit()

    assert paid["amount"] == "1500.00"
    row = session.execute(
        text(
            "SELECT p.status, p.operation_id, o.type_id, ot.direction, o.amount "
            "FROM payroll_payments p "
            "JOIN operations o ON o.id = p.operation_id "
            "JOIN operation_types ot ON ot.id = o.type_id "
            "WHERE p.id = :i"
        ),
        {"i": paid["payment_id"]},
    ).one()
    assert row.status == "POSTED"
    # OUT operations are stored with negative sign (§sign convention).
    assert row.direction == "OUT" and float(row.amount) == -1500.00
    assert row.type_id == session.scalar(
        text("SELECT id FROM operation_types WHERE code = 'SALARY_PAYMENT'")
    )

    summary_status = session.scalar(
        text("SELECT status FROM payroll_summary WHERE id = :s"), {"s": target["id"]}
    )
    assert summary_status == "PAID"
    period_status = session.scalar(
        text("SELECT status FROM payroll_periods WHERE id = :p"), {"p": ids["period_id"]}
    )
    # TD-020 (Option A): period PAID is derived — one of two summaries paid,
    # period must NOT be PAID yet.
    assert period_status != "PAID"
    paid_entries = session.execute(
        text(
            "SELECT count(*) FROM work_entries WHERE employee_id = :e "
            "AND status = 'PAID' AND payroll_period_id = :p"
        ),
        {"e": ids["piecework_emp"], "p": ids["period_id"]},
    ).scalar_one()
    assert paid_entries == 2, "both CONFIRMED entries freeze as PAID; DRAFT stays DRAFT"


def test_period_paid_only_when_all_summaries_paid(session: Session, engine: Engine):
    """TD-020 (Option A): period.status becomes PAID only after ALL summaries
    in the period are paid. Derived status, not set by a single payment."""
    ids = _seed_payroll(engine)
    summaries = calculate_payroll(session, ids["period_id"])
    session.commit()

    for s in summaries:
        approve_payroll_summary(session, int(s["id"]))
    session.commit()

    # Pay first summary — period must stay OPEN.
    pay_payroll_summary(session, int(summaries[0]["id"]), date(2026, 4, 5), "CASH")
    session.commit()
    assert (
        session.scalar(
            text("SELECT status FROM payroll_periods WHERE id = :p"),
            {"p": ids["period_id"]},
        )
        != "PAID"
    )

    # Pay remaining summaries — period becomes PAID (derived) only after ALL are paid.
    for s in summaries[1:]:
        pay_payroll_summary(session, int(s["id"]), date(2026, 4, 6), "CASH")
    session.commit()
    assert (
        session.scalar(
            text("SELECT status FROM payroll_periods WHERE id = :p"),
            {"p": ids["period_id"]},
        )
        == "PAID"
    )


def test_double_payment_and_status_guards_blocked(session: Session, engine: Engine):
    """§57 — no second payment, and only APPROVED summaries can be paid."""
    import pytest

    ids = _seed_payroll(engine)
    summaries = calculate_payroll(session, ids["period_id"])
    session.commit()
    target = next(r for r in summaries if r["employee_id"] == ids["piecework_emp"])

    d1 = __import__("datetime").date(2026, 4, 5)
    d2 = __import__("datetime").date(2026, 4, 6)

    # The session joins the fixture transaction, so session.rollback() would
    # discard the CALCULATED rows too; failing attempts run inside savepoints.
    with pytest.raises(ValueError, match="APPROVED"):
        sp = session.begin_nested()
        try:
            pay_payroll_summary(session, int(target["id"]), d1, "CASH")
        finally:
            sp.rollback()

    approve_payroll_summary(session, int(target["id"]))
    pay_payroll_summary(session, int(target["id"]), d1, "CASH")
    session.commit()

    with pytest.raises(ValueError, match="double payment blocked"):
        sp = session.begin_nested()
        try:
            pay_payroll_summary(session, int(target["id"]), d2, "CASH")
        finally:
            sp.rollback()

    with pytest.raises(ValueError, match="already PAID"):
        approve_payroll_summary(session, int(target["id"]))


def test_payroll_api_flow_and_rbac(engine: Engine, applied_schema, admin_headers):
    """§53/§36 — API: calculate → approve → pay → double pay 422; RBAC enforced."""
    ids = _seed_payroll(engine)
    with TestClient(app) as client:
        r = client.post(
            "/payroll/calculate",
            headers=admin_headers,
            json={"period_id": ids["period_id"]},
        )
        assert r.status_code == 200, r.text
        summaries = r.json()["summaries"]
        target = next(s for s in summaries if s["employee_id"] == ids["piecework_emp"])

        r = client.post(f"/payroll/summaries/{target['id']}/approve", headers=admin_headers)
        assert r.status_code == 200, r.text

        r = client.post(
            "/payroll/pay",
            headers=admin_headers,
            json={
                "summary_id": target["id"],
                "payment_date": "2026-04-05",
                "payment_method": "KASPI_TRANSFER",
            },
        )
        assert r.status_code == 200, r.text
        payment = r.json()
        assert payment["amount"] == "1500.00" and payment["operation_id"] > 0

        # §57 — the second pay attempt is refused at the API boundary too
        r = client.post(
            "/payroll/pay",
            headers=admin_headers,
            json={
                "summary_id": target["id"],
                "payment_date": "2026-04-06",
                "payment_method": "CASH",
            },
        )
        assert r.status_code == 422, r.text

        # RBAC: anonymous → 401, VIEWER → 403
        assert (
            client.post("/payroll/calculate", json={"period_id": ids["period_id"]}).status_code
            == 401
        )

        username = f"payroll-viewer-{uuid.uuid4().hex[:8]}"
        r = client.post(
            "/auth/users",
            headers=admin_headers,
            json={"username": username, "password": "Viewer-Pass-123", "roles": ["VIEWER"]},
        )
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"username": username, "password": "Viewer-Pass-123"})
        viewer = {"Authorization": f"Bearer {r.json()['access_token']}"}
        assert (
            client.post(
                "/payroll/calculate", headers=viewer, json={"period_id": ids["period_id"]}
            ).status_code
            == 403
        )


def test_payroll_overpayment_blocked_by_trigger(engine, applied_schema):
    """P2-1: DB trigger blocks payroll payments exceeding the summary total,
    even via direct SQL (defense in depth beyond the service layer)."""
    ids = _seed_payroll(engine)
    with engine.begin() as conn:
        # Create a summary with total_calculated = 100.00 (GENERATED from components)
        summary_id = conn.execute(
            text(
                "INSERT INTO payroll_summary "
                "(employee_id, payroll_period_id, ip_id, piecework_total, base_salary, "
                "bonuses, deductions, status) "
                "VALUES (:e, :p, :ip, 100.00, 0, 0, 0, 'APPROVED') RETURNING id"
            ),
            {"e": ids["piecework_emp"], "p": ids["period_id"], "ip": ids["ip_id"]},
        ).scalar_one()
        # First payment of 100.00 is fine
        conn.execute(
            text(
                "INSERT INTO payroll_payments "
                "(payroll_summary_id, payment_date, amount, payment_method, status) "
                "VALUES (:s, '2026-04-05', 100.00, 'CASH', 'POSTED')"
            ),
            {"s": summary_id},
        )
        # Second payment would overpay → trigger blocks
        with pytest.raises(Exception, match="overpayment blocked"):
            conn.execute(
                text(
                    "INSERT INTO payroll_payments "
                    "(payroll_summary_id, payment_date, amount, payment_method, status) "
                    "VALUES (:s, '2026-04-06', 1.00, 'CASH', 'POSTED')"
                ),
                {"s": summary_id},
            )
