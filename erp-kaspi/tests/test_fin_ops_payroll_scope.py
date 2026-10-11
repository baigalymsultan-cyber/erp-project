from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.api import app
from app.core.security import hash_password

PASSWORD = "FinOps-Payroll-Test-2026!"


def _insert_user(conn, role: str, employee_id: int, suffix: str) -> str:
    username = f"finops-{role.lower()}-{suffix}"
    user_id = conn.execute(
        text(
            "INSERT INTO users (username, password_hash, employee_id, is_active) "
            "VALUES (:username, :password_hash, :employee_id, TRUE) RETURNING id"
        ),
        {
            "username": username,
            "password_hash": hash_password(PASSWORD),
            "employee_id": employee_id,
        },
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO user_roles (user_id, role_id) "
            "SELECT :user_id, id FROM roles WHERE code = :role"
        ),
        {"user_id": user_id, "role": role},
    )
    return username


def _seed_scope(engine: Engine) -> dict[str, int | str]:
    suffix = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        ip_a, ip_b = (
            int(
                conn.execute(
                    text(
                        "INSERT INTO ip_entities (name, bin) VALUES (:name, :bin) "
                        "RETURNING id"
                    ),
                    {
                        "name": f"FINOPS payroll {suffix} {index}",
                        "bin": f"{(int(suffix, 16) + index) % 10**12:012d}",
                    },
                ).scalar_one()
            )
            for index in (1, 2)
        )
        employee_a = int(
            conn.execute(
                text(
                    "INSERT INTO employees "
                    "(full_name, assigned_ip_id, payment_type, base_salary) "
                    "VALUES (:name, :ip_id, 'SALARY', 1000.00) RETURNING id"
                ),
                {"name": f"FINOPS salary A {suffix}", "ip_id": ip_a},
            ).scalar_one()
        )
        employee_b = int(
            conn.execute(
                text(
                    "INSERT INTO employees "
                    "(full_name, assigned_ip_id, payment_type, base_salary) "
                    "VALUES (:name, :ip_id, 'SALARY', 2000.00) RETURNING id"
                ),
                {"name": f"FINOPS salary B {suffix}", "ip_id": ip_b},
            ).scalar_one()
        )
        order_a = int(
            conn.execute(
                text(
                    "INSERT INTO orders (ip_id, external_order_id, order_date) "
                    "VALUES (:ip_id, :external_id, TIMESTAMPTZ '2099-01-01') RETURNING id"
                ),
                {"ip_id": ip_a, "external_id": f"FINOPS-A-{suffix}"},
            ).scalar_one()
        )
        order_b = int(
            conn.execute(
                text(
                    "INSERT INTO orders (ip_id, external_order_id, order_date) "
                    "VALUES (:ip_id, :external_id, TIMESTAMPTZ '2099-01-01') RETURNING id"
                ),
                {"ip_id": ip_b, "external_id": f"FINOPS-B-{suffix}"},
            ).scalar_one()
        )
        work_type_id = int(
            conn.execute(
                text(
                    "INSERT INTO work_operation_types (code, name, unit, calc_mode) "
                    "VALUES (:code, :name, 'pcs', 'per_unit') RETURNING id"
                ),
                {"code": f"FINOPS_{suffix}", "name": f"FINOPS work {suffix}"},
            ).scalar_one()
        )
        conn.execute(
            text(
                "INSERT INTO rates_history "
                "(work_operation_type_id, rate, effective_from) "
                "VALUES (:work_type_id, 10.00, TIMESTAMPTZ '2000-01-01 00:00:00+00')"
            ),
            {"work_type_id": work_type_id},
        )
        brigadier = _insert_user(conn, "BRIGADIER", employee_a, suffix)
        accountant = _insert_user(conn, "ACCOUNTANT", employee_a, suffix)
        director = _insert_user(conn, "DIRECTOR", employee_a, suffix)

    return {
        "suffix": suffix,
        "ip_a": ip_a,
        "ip_b": ip_b,
        "employee_a": employee_a,
        "employee_b": employee_b,
        "order_a": order_a,
        "order_b": order_b,
        "work_type_id": work_type_id,
        "brigadier": brigadier,
        "accountant": accountant,
        "director": director,
    }


def _login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _new_period(conn, suffix: str) -> tuple[int, date, date]:
    month_index = int(suffix[:8], 16) % (49 * 12)
    while True:
        year = 2050 + month_index // 12
        month = month_index % 12 + 1
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        existing = conn.execute(
            text(
                "SELECT 1 FROM payroll_periods "
                "WHERE start_date <= :end AND end_date >= :start"
            ),
            {"start": start, "end": end},
        ).scalar_one_or_none()
        if existing is None:
            break
        month_index = (month_index + 1) % (49 * 12)
    period_id = int(
        conn.execute(
            text(
                "INSERT INTO payroll_periods (start_date, end_date) "
                "VALUES (:start, :end) RETURNING id"
            ),
            {"start": start, "end": end},
        ).scalar_one()
    )
    return period_id, start, end


def test_work_entry_create_enforces_employee_and_order_ip_scope(
    engine: Engine, applied_schema, admin_headers
):
    ids = _seed_scope(engine)
    base = {
        "employee_id": ids["employee_a"],
        "work_operation_type_id": ids["work_type_id"],
        "ip_id": ids["ip_a"],
        "entry_date": "2099-01-05",
        "quantity": 2,
    }

    with TestClient(app) as client:
        brigadier_headers = _login(client, str(ids["brigadier"]))
        allowed = client.post(
            "/payroll/work-entries",
            json={**base, "order_id": ids["order_a"]},
            headers=brigadier_headers,
        )
        foreign_employee = client.post(
            "/payroll/work-entries",
            json={**base, "employee_id": ids["employee_b"]},
            headers=brigadier_headers,
        )
        foreign_order = client.post(
            "/payroll/work-entries",
            json={**base, "order_id": ids["order_b"]},
            headers=brigadier_headers,
        )
        foreign_ip = client.post(
            "/payroll/work-entries",
            json={**base, "ip_id": ids["ip_b"]},
            headers=brigadier_headers,
        )
        admin_allowed = client.post(
            "/payroll/work-entries",
            json={
                **base,
                "employee_id": ids["employee_b"],
                "ip_id": ids["ip_b"],
                "order_id": ids["order_b"],
            },
            headers=admin_headers,
        )
        admin_mismatch = client.post(
            "/payroll/work-entries",
            json={**base, "ip_id": ids["ip_b"]},
            headers=admin_headers,
        )
        admin_order_mismatch = client.post(
            "/payroll/work-entries",
            json={**base, "order_id": ids["order_b"]},
            headers=admin_headers,
        )
        director_headers = _login(client, str(ids["director"]))
        director_allowed = client.post(
            "/payroll/work-entries",
            json={
                **base,
                "employee_id": ids["employee_b"],
                "ip_id": ids["ip_b"],
                "order_id": ids["order_b"],
            },
            headers=director_headers,
        )

    assert allowed.status_code == 201, allowed.text
    assert foreign_employee.status_code == 403, foreign_employee.text
    assert foreign_order.status_code == 403, foreign_order.text
    assert foreign_ip.status_code == 403, foreign_ip.text
    assert admin_allowed.status_code == 201, admin_allowed.text
    assert admin_mismatch.status_code == 422, admin_mismatch.text
    assert admin_order_mismatch.status_code == 422, admin_order_mismatch.text
    assert director_allowed.status_code == 201, director_allowed.text


def test_payroll_calculation_is_scoped_and_uses_employee_ip(
    engine: Engine, applied_schema, admin_headers
):
    ids = _seed_scope(engine)
    with engine.begin() as conn:
        period_id, start, _ = _new_period(conn, str(ids["suffix"]))
        for employee_id, ip_id, order_id, quantity in (
            (ids["employee_a"], ids["ip_a"], None, 2),
            (ids["employee_a"], ids["ip_b"], None, 100),
            (ids["employee_a"], ids["ip_a"], ids["order_b"], 50),
            (ids["employee_b"], ids["ip_b"], None, 3),
            (ids["employee_b"], ids["ip_a"], None, 25),
        ):
            conn.execute(
                text(
                    "INSERT INTO work_entries "
                    "(employee_id, work_operation_type_id, order_id, ip_id, entry_date, "
                    "quantity, status) VALUES (:employee_id, :work_type_id, :order_id, "
                    ":ip_id, :entry_date, :quantity, 'CONFIRMED')"
                ),
                {
                    "employee_id": employee_id,
                    "work_type_id": ids["work_type_id"],
                    "order_id": order_id,
                    "ip_id": ip_id,
                    "entry_date": start,
                    "quantity": quantity,
                },
            )

    with TestClient(app) as client:
        accountant_headers = _login(client, str(ids["accountant"]))
        scoped = client.post(
            "/payroll/calculate",
            json={"period_id": period_id},
            headers=accountant_headers,
        )
        owned_summary = client.get(
            "/payroll/summary",
            params={"employee_id": ids["employee_a"]},
            headers=accountant_headers,
        )
        foreign_adjustment = client.post(
            "/payroll/calculate",
            json={
                "period_id": period_id,
                "adjustments": [{"employee_id": ids["employee_b"], "bonuses": "1.00"}],
            },
            headers=accountant_headers,
        )
        global_result = client.post(
            "/payroll/calculate",
            json={"period_id": period_id},
            headers=admin_headers,
        )
        director_headers = _login(client, str(ids["director"]))
        director_result = client.post(
            "/payroll/calculate",
            json={"period_id": period_id},
            headers=director_headers,
        )
        foreign_work_entries = client.get(
            "/reports/work-entries",
            params={"employee_id": ids["employee_b"]},
            headers=accountant_headers,
        )
        scoped_work_entries = client.get(
            "/reports/work-entries",
            params={"date_from": start.isoformat(), "date_to": start.isoformat()},
            headers=accountant_headers,
        )
        scoped_work_summary = client.get(
            "/reports/work-entries/summary",
            params={"date_from": start.isoformat(), "date_to": start.isoformat()},
            headers=accountant_headers,
        )

    assert scoped.status_code == 200, scoped.text
    scoped_rows = scoped.json()["summaries"]
    assert [row["employee_id"] for row in scoped_rows] == [ids["employee_a"]]
    assert Decimal(scoped_rows[0]["piecework_total"]) == Decimal("20.00")
    assert Decimal(scoped_rows[0]["base_salary"]) == Decimal("1000.00")
    assert owned_summary.status_code == 200, owned_summary.text
    assert [row["id"] for row in owned_summary.json()["periods"]] == [scoped_rows[0]["id"]]
    assert foreign_adjustment.status_code == 403, foreign_adjustment.text
    assert global_result.status_code == 200, global_result.text
    global_employee_ids = {row["employee_id"] for row in global_result.json()["summaries"]}
    assert {ids["employee_a"], ids["employee_b"]} <= global_employee_ids
    assert director_result.status_code == 200, director_result.text
    director_employee_ids = {row["employee_id"] for row in director_result.json()["summaries"]}
    assert {ids["employee_a"], ids["employee_b"]} <= director_employee_ids
    assert foreign_work_entries.status_code == 403, foreign_work_entries.text
    assert scoped_work_entries.status_code == 200, scoped_work_entries.text
    assert len(scoped_work_entries.json()["items"]) == 1
    assert scoped_work_entries.json()["items"][0]["employee_id"] == ids["employee_a"]
    assert Decimal(scoped_work_entries.json()["items"][0]["total_sum"]) == Decimal("20.00")
    assert scoped_work_summary.status_code == 200, scoped_work_summary.text
    assert scoped_work_summary.json() == {"count": 1, "total_sum": "20.00"}


def test_payroll_summary_read_approve_and_pay_check_employee_scope(
    engine: Engine, applied_schema, admin_headers
):
    ids = _seed_scope(engine)
    with engine.begin() as conn:
        period_id, period_start, _ = _new_period(conn, str(ids["suffix"]))
        employee_c = int(
            conn.execute(
                text(
                    "INSERT INTO employees "
                    "(full_name, assigned_ip_id, payment_type, base_salary) "
                    "VALUES (:name, :ip_id, 'SALARY', 3000.00) RETURNING id"
                ),
                {"name": f"FINOPS salary C {ids['suffix']}", "ip_id": ids["ip_b"]},
            ).scalar_one()
        )
        work_entry_ids = {}
        for key, ip_id, order_id, quantity in (
            ("valid", ids["ip_b"], None, 3),
            ("wrong_ip", ids["ip_a"], None, 50),
            ("wrong_order", ids["ip_b"], ids["order_a"], 100),
        ):
            work_entry_ids[key] = int(
                conn.execute(
                    text(
                        "INSERT INTO work_entries "
                        "(employee_id, work_operation_type_id, order_id, ip_id, entry_date, "
                        "quantity, status) VALUES (:employee_id, :work_type_id, :order_id, "
                        ":ip_id, :entry_date, :quantity, 'CONFIRMED') RETURNING id"
                    ),
                    {
                        "employee_id": employee_c,
                        "work_type_id": ids["work_type_id"],
                        "order_id": order_id,
                        "ip_id": ip_id,
                        "entry_date": period_start,
                        "quantity": quantity,
                    },
                ).scalar_one()
            )
        summary_calculated = int(
            conn.execute(
                text(
                    "INSERT INTO payroll_summary "
                    "(payroll_period_id, employee_id, ip_id, piecework_total, base_salary, "
                    "bonuses, deductions, status) VALUES (:period_id, :employee_id, :ip_id, "
                    "12.00, 2000.00, 0, 0, 'CALCULATED') RETURNING id"
                ),
                {
                    "period_id": period_id,
                    "employee_id": ids["employee_b"],
                    "ip_id": ids["ip_b"],
                },
            ).scalar_one()
        )
        summary_approved = int(
            conn.execute(
                text(
                    "INSERT INTO payroll_summary "
                    "(payroll_period_id, employee_id, ip_id, piecework_total, base_salary, "
                    "bonuses, deductions, status) VALUES (:period_id, :employee_id, :ip_id, "
                    "0, 3000.00, 0, 0, 'APPROVED') RETURNING id"
                ),
                {
                    "period_id": period_id,
                    "employee_id": employee_c,
                    "ip_id": ids["ip_b"],
                },
            ).scalar_one()
        )

    payment_comment = f"FINOPS payroll payment {ids['suffix']}"
    with TestClient(app) as client:
        accountant_headers = _login(client, str(ids["accountant"]))
        foreign_read = client.get(
            "/payroll/summary",
            params={"employee_id": ids["employee_b"]},
            headers=accountant_headers,
        )
        foreign_approve = client.post(
            f"/payroll/summaries/{summary_calculated}/approve",
            headers=accountant_headers,
        )
        foreign_pay = client.post(
            "/payroll/pay",
            json={
                "summary_id": summary_approved,
                "payment_date": "2099-03-01",
                "payment_method": "CASH",
                "comment": payment_comment,
            },
            headers=accountant_headers,
        )
        with engine.connect() as conn:
            operation_count_after_denied_pay = conn.execute(
                text("SELECT count(*) FROM operations WHERE comment = :comment"),
                {"comment": payment_comment},
            ).scalar_one()
        foreign_due = client.get(
            "/reports/payroll-due",
            params={"ip_id": ids["ip_a"]},
            headers=accountant_headers,
        )
        cross_ip_due = client.get(
            "/reports/payroll-due",
            params={"ip_id": ids["ip_b"]},
            headers=accountant_headers,
        )
        foreign_due_summary = client.get(
            "/reports/payroll-due/summary",
            headers=accountant_headers,
        )
        admin_approve = client.post(
            f"/payroll/summaries/{summary_calculated}/approve",
            headers=admin_headers,
        )
        admin_pay = client.post(
            "/payroll/pay",
            json={
                "summary_id": summary_approved,
                "payment_date": "2099-03-01",
                "payment_method": "CASH",
                "comment": payment_comment,
            },
            headers=admin_headers,
        )

    assert foreign_read.status_code == 403, foreign_read.text
    assert foreign_approve.status_code == 403, foreign_approve.text
    assert foreign_pay.status_code == 403, foreign_pay.text
    assert foreign_due.status_code == 200, foreign_due.text
    assert foreign_due.json()["items"] == []
    assert cross_ip_due.status_code == 403, cross_ip_due.text
    assert foreign_due_summary.status_code == 200, foreign_due_summary.text
    assert {key: Decimal(value) for key, value in foreign_due_summary.json().items()} == {
        "calculated": Decimal("0.00"),
        "paid": Decimal("0.00"),
        "due": Decimal("0.00"),
    }
    assert operation_count_after_denied_pay == 0
    assert admin_approve.status_code == 200, admin_approve.text
    assert admin_approve.json()["status"] == "APPROVED"
    assert admin_pay.status_code == 200, admin_pay.text
    with engine.connect() as conn:
        payment = conn.execute(
            text(
                "SELECT o.ip_id, o.amount, pp.status AS payment_status, "
                "ps.status AS summary_status "
                "FROM operations o JOIN payroll_payments pp ON pp.operation_id = o.id "
                "JOIN payroll_summary ps ON ps.id = pp.payroll_summary_id "
                "WHERE o.comment = :comment"
            ),
            {"comment": payment_comment},
        ).one()
        paid_statuses = dict(
            conn.execute(
                text(
                    "SELECT id, status FROM work_entries WHERE id IN "
                    "(:valid, :wrong_ip, :wrong_order)"
                ),
                {
                    "valid": work_entry_ids["valid"],
                    "wrong_ip": work_entry_ids["wrong_ip"],
                    "wrong_order": work_entry_ids["wrong_order"],
                },
            ).all()
        )
    assert payment.ip_id == ids["ip_b"]
    assert Decimal(payment.amount) == Decimal("-3000.00")
    assert payment.payment_status == "POSTED"
    assert payment.summary_status == "PAID"
    assert paid_statuses == {
        work_entry_ids["valid"]: "PAID",
        work_entry_ids["wrong_ip"]: "CONFIRMED",
        work_entry_ids["wrong_order"]: "CONFIRMED",
    }
