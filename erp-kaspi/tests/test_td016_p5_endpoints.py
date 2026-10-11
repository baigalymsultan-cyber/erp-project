"""TD-016 P5 — summary/reference endpoints backing the API-only UI.

Covers the endpoints added so the Streamlit modules can drop direct DB
access entirely (REQ-TD016-04):

  * E18 ``GET /ips``            — ИП picker (allow-list scoped, API1);
  * E19 ``GET /operations/summary``      — A10 KPI (income/expense/count);
  * E20 ``GET /orders/summary``          — A15 KPI (count/total/by_status);
  * E21 ``GET /products/summary``        — A16 KPI (count/avg_price);
  * E22 ``GET /reports/work-entries/summary`` — A17 KPI (count/total_sum);
  * E23 ``GET /reports/payroll-due/summary``  — A19 KPI (calc/paid/due);
  * E24 ``/audit-log`` action + created date filters (C5 parity).

Every endpoint: 401 without token, server-side ИП isolation, Decimal-safe
string amounts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import app
from app.core.security import hash_password

RUN = uuid.uuid4().hex[:10]
BIN_BASE = 902_000_000_000 + (int(RUN, 16) % 899_000_000) * 100


def _client() -> TestClient:
    return TestClient(app)


def _login(client: TestClient, username: str, password: str) -> str:
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def _fresh_rate_limit():
    from app.core.security import reset_auth_rate_limit

    reset_auth_rate_limit()
    yield
    reset_auth_rate_limit()


@pytest.fixture(scope="module")
def world(engine):
    """Two ИП, operations on both, orders, products, a work entry + payroll
    summary on ИП-1, an ADMIN, and a USER bound only to ИП-1."""
    with engine.begin() as conn:
        ip1 = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": f"P5A-{RUN}", "b": str(BIN_BASE + 1)},
        ).scalar_one()
        ip2 = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": f"P5B-{RUN}", "b": str(BIN_BASE + 2)},
        ).scalar_one()

        sale = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_SHOP'")
        ).scalar_one()
        rent = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
        ).scalar_one()

        for ip, type_id, amount in ((ip1, sale, "1500.00"), (ip1, rent, "-400.00"),
                                    (ip2, sale, "9900.00")):
            conn.execute(
                text(
                    "INSERT INTO operations (ip_id, type_id, operation_date, accrual_date, amount,"
                    " status, comment) VALUES (:ip, :t, CURRENT_DATE, CURRENT_DATE, :a, 'POSTED',"
                    " :c)"
                ),
                {"ip": ip, "t": type_id, "a": amount, "c": f"P5 op {RUN}"},
            )

        admin = f"p5-admin-{RUN}"
        user = f"p5-user-{RUN}"
        pw = "P5-Pass-2026!"
        uid_admin = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, is_active)"
                " VALUES (:u, :h, TRUE) RETURNING id"
            ),
            {"u": admin, "h": hash_password(pw)},
        ).scalar_one()
        uid_user = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, is_active)"
                " VALUES (:u, :h, TRUE) RETURNING id"
            ),
            {"u": user, "h": hash_password(pw)},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id)"
                " SELECT :u, id FROM roles WHERE code = 'ADMIN'"
            ),
            {"u": uid_admin},
        )
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id)"
                " SELECT :u, id FROM roles WHERE code = 'VIEWER'"
            ),
            {"u": uid_user},
        )
        emp_id = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
                "VALUES (:n, :ip, 'PIECEWORK') RETURNING id"
            ),
            {"n": f"P5 Emp {RUN}", "ip": ip1},
        ).scalar_one()
        conn.execute(
            text("UPDATE users SET employee_id = :e WHERE id = :u"),
            {"e": emp_id, "u": uid_user},
        )

        emp = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type,"
                " base_salary, status) VALUES (:n, :ip, 'SALARY', 100, 'ACTIVE')"
                " RETURNING id"
            ),
            {"n": f"P5 Emp {RUN}", "ip": ip1},
        ).scalar_one()
        conn.execute(
            text(
                "UPDATE users SET employee_id = :e WHERE id = :u"
            ),
            {"e": emp, "u": uid_user},
        )
        wot = conn.execute(
            text("SELECT id FROM work_operation_types WHERE is_active LIMIT 1")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO work_entries (ip_id, employee_id, work_operation_type_id,"
                " entry_date, quantity, rate_at_time, status)"
                " VALUES (:ip, :e, :w, CURRENT_DATE, 2, 50, 'CONFIRMED')"
            ),
            {"ip": ip1, "e": emp, "w": wot},
        )
        period = conn.execute(
            text(
                "INSERT INTO payroll_periods (start_date, end_date, status)"
                " VALUES (date_trunc('month', CURRENT_DATE)::date,"
                " (date_trunc('month', CURRENT_DATE) + interval '1 month - 1 day')"
                " ::date, 'OPEN') RETURNING id"
            ),
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO payroll_summary (payroll_period_id, employee_id, ip_id,"
                " piecework_total, base_salary, bonuses, deductions, status)"
                " VALUES (:p, :e, :ip, 100, 100, 0, 0, 'CALCULATED')"
            ),
            {"p": period, "e": emp, "ip": ip1},
        )
        summary_id = conn.execute(
            text("SELECT id FROM payroll_summary WHERE employee_id = :e"), {"e": emp}
        ).scalar_one()
        # v_payroll_due computes amount_paid from POSTED payroll_payments (§36),
        # not from a denormalised column — seed a payment row.
        conn.execute(
            text(
                "INSERT INTO payroll_payments (payroll_summary_id, payment_date, amount, "
                "payment_method, status) VALUES (:s, CURRENT_DATE, 50, 'CASH', 'POSTED')"
            ),
            {"s": summary_id},
        )

        cn = f"P5 customer {RUN}"
        for ext, status, total in (("P5-ORD-1", "NEW", "5000.00"),
                                   ("P5-ORD-2", "COMPLETED", "7000.00"),
                                   ("P5-ORD-3", "NEW", "3000.00")):
            conn.execute(
                text(
                    "INSERT INTO orders (ip_id, external_order_id, customer_name,"
                    " order_date, status, total_amount)"
                    " VALUES (:ip, :x, :cn, CURRENT_DATE, :s, :t)"
                ),
                {"ip": ip1 if ext != "P5-ORD-3" else ip2, "x": ext, "cn": cn,
                 "s": status, "t": total},
            )

        for sku, price in ((f"P5-SKU-1-{RUN}", "100.00"), (f"P5-SKU-2-{RUN}", "300.00")):
            conn.execute(
                text(
                    "INSERT INTO products (sku, name, unit, selling_price, is_active)"
                    " VALUES (:s, :n, 'шт', :p, TRUE)"
                ),
                {"s": sku, "n": f"P5 product {RUN}", "p": price},
            )

    return {"ip1": ip1, "ip2": ip2, "admin": admin, "user": user, "pw": pw}


@pytest.mark.parametrize(
    "endpoint",
    [
        "/ips",
        "/operations/summary",
        "/orders/summary",
        "/products/summary",
        "/reports/work-entries/summary",
        "/reports/payroll-due/summary",
        "/audit-log",
    ],
)
def test_p5_01_requires_token(engine, applied_schema, endpoint):
    with _client() as client:
        r = client.get(endpoint)
    assert r.status_code == 401, f"{endpoint}: {r.text}"


@pytest.mark.usefixtures("_fresh_rate_limit")
def test_p5_02_ips_allow_list(engine, applied_schema, world):
    with _client() as client:
        admin = _login(client, world["admin"], world["pw"])
        user = _login(client, world["user"], world["pw"])
        ra = client.get("/ips", headers=_auth(admin))
        ru = client.get("/ips", headers=_auth(user))
    assert ra.status_code == 200 and ru.status_code == 200, ra.text
    a_ids = {r["id"] for r in ra.json()["items"]}
    u_ids = {r["id"] for r in ru.json()["items"]}
    assert {world["ip1"], world["ip2"]} <= a_ids
    assert u_ids == {world["ip1"]}, f"leaked: {u_ids}"


def test_p5_03_operations_summary(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        today = datetime.now(timezone.utc).date().isoformat()  # UTC: matches DB CURRENT_DATE (app forces UTC)
        r = client.get(
            "/operations/summary",
            params={"period_start": today, "period_end": today, "q": f"P5 op {RUN}"},
            headers=_auth(token),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # §27 signs: income positive, expense stored negative → expense = 400.
    assert Decimal(body["income"]) == Decimal("11400.00"), body
    assert Decimal(body["expense"]) == Decimal("400.00"), body
    assert body["count"] == 3


def test_p5_04_operations_summary_restricted(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["user"], world["pw"])
        today = datetime.now(timezone.utc).date().isoformat()  # UTC: matches DB CURRENT_DATE (app forces UTC)
        r = client.get(
            "/operations/summary",
            params={"period_start": today, "period_end": today, "q": f"P5 op {RUN}"},
            headers=_auth(token),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["income"]) == Decimal("1500.00"), body
    assert body["count"] == 2


@pytest.mark.usefixtures("_fresh_rate_limit")
def test_p5_05_orders_summary(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        r = client.get("/orders/summary", params={"q": f"P5 customer {RUN}"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3, body
    assert Decimal(body["total_amount"]) == Decimal("15000.00"), body
    by_status = {row["status"]: row["n"] for row in body["by_status"]}
    assert by_status == {"NEW": 2, "COMPLETED": 1}, body


def test_p5_06_products_summary(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        r = client.get("/products/summary", params={"q": f"P5 product {RUN}"},
                       headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2, body
    assert Decimal(body["avg_price"]) == Decimal("200.00"), body


def test_p5_07_work_entries_summary(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        today = datetime.now(timezone.utc).date().isoformat()  # UTC: matches DB CURRENT_DATE (app forces UTC)
        r = client.get(
            "/reports/work-entries/summary",
            params={"date_from": today, "date_to": today},
            headers=_auth(token),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] >= 1, body
    assert Decimal(body["total_sum"]) >= Decimal("100.00"), body


@pytest.mark.usefixtures("_fresh_rate_limit")
def test_p5_08_payroll_due_summary(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        r = client.get("/reports/payroll-due/summary", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["calculated"]) >= Decimal("200.00"), body
    assert Decimal(body["paid"]) >= Decimal("50.00"), body
    assert Decimal(body["due"]) >= Decimal("150.00"), body


@pytest.mark.usefixtures("_fresh_rate_limit")
def test_p5_09_audit_log_action_and_date_filters(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        base = {"limit": 200}
        all_rows = client.get("/audit-log", params=base, headers=_auth(token)).json()
        assert all_rows["total"] >= 1
        r = client.get(
            "/audit-log",
            params={**base, "action": "LOGIN"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        actions = {row["action"] for row in r.json()["items"]}
        assert actions <= {"LOGIN"}, r.json()
        # Date window that excludes everything.
        r2 = client.get(
            "/audit-log",
            params={**base, "created_from": "2001-01-01", "created_to": "2001-01-02"},
            headers=_auth(token),
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["total"] == 0, r2.json()


def test_p5_10_restricted_audit_log_403(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["user"], world["pw"])
        r = client.get("/audit-log", headers=_auth(token))
    assert r.status_code == 403, r.text
