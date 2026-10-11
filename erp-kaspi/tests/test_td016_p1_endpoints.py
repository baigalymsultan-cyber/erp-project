"""TD-016 P1 — read-only list endpoints (E1/E2/E3/E4).

REQ-TD016-01: 401 without token on every new endpoint.
REQ-TD016-02: server-side ИП isolation (BOLA — a restricted principal can
neither widen the filter nor see another ИП's rows).
REQ-TD016-03: server paging with total.
REQ-TD016-05: amounts are Decimal-safe strings; mypy-clean schemas.
REQ-TD016-10: positive + negative tests per endpoint.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import app
from app.core.security import hash_password

RUN = uuid.uuid4().hex[:10]
#: 12-значный БИН, уникальный для прогона (900000000000..999999999999).
BIN_BASE = 900000000000 + (int(RUN, 16) % 99_999_999_999)


def _client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    """Each test gets a clean in-process auth limiter (§55)."""
    from app.core.security import reset_auth_rate_limit

    reset_auth_rate_limit()
    yield
    reset_auth_rate_limit()


@pytest.fixture(scope="module")
def two_ips(engine, applied_schema):
    """Two ИП + an ADMIN and a restricted USER bound only to ИП-1."""
    with engine.begin() as conn:
        ip1 = conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"
            ),
            {"n": f"P1A-{RUN}", "b": str(BIN_BASE + 1)},
        ).scalar_one()
        ip2 = conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"
            ),
            {"n": f"P1B-{RUN}", "b": str(BIN_BASE + 2)},
        ).scalar_one()
        admin = f"p1-admin-{RUN}"
        uid_admin = conn.execute(
            text(
                "INSERT INTO users (username, password_hash) VALUES (:u, :h)"
                " RETURNING id"
            ),
            {"u": admin, "h": hash_password("P1-Admin-2026!")},
        ).scalar_one()
        restricted = f"p1-user-{RUN}"
        uid_user = conn.execute(
            text(
                "INSERT INTO users (username, password_hash) VALUES (:u, :h)"
                " RETURNING id"
            ),
            {"u": restricted, "h": hash_password("P1-User-2026!")},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id)"
                " SELECT :uid, id FROM roles WHERE code = 'ADMIN'"
            ),
            {"uid": uid_admin},
        )
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id)"
                " SELECT :uid, id FROM roles WHERE code = 'VIEWER'"
            ),
            {"uid": uid_user},
        )
        emp = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary)"
                " VALUES (:n, :ip, 'SALARY', 0) RETURNING id"
            ),
            {"n": f"P1 employee {RUN}", "ip": ip1},
        ).scalar_one()
        conn.execute(
            text("UPDATE users SET employee_id = :e WHERE id = :u"),
            {"e": emp, "u": uid_user},
        )
        # one posted operation in each ИП
        type_row = conn.execute(
            text("SELECT id FROM operation_types WHERE kind = 'INCOME' LIMIT 1")
        ).scalar_one()
        for ip in (ip1, ip2):
            conn.execute(
                text(
                    "INSERT INTO operations (ip_id, type_id, amount,"
                    " operation_date, accrual_date, comment, status)"
                    " VALUES (:ip, :t, 1500.00, '2026-09-01', '2026-09-01', :c, 'POSTED')"
                ),
                {"ip": ip, "t": type_row, "c": f"seed-{RUN}"},
            )
        # one order in each ИП (orders list + statuses coverage)
        for ip in (ip1, ip2):
            conn.execute(
                text(
                    "INSERT INTO orders (ip_id, external_order_id, customer_name,"
                    " status, source, total_amount, kaspi_commission, delivery_fee,"
                    " order_date)"
                    " VALUES (:ip, :ext, :cust, 'NEW', 'MANUAL', 5000.00, 250.00, 0,"
                    " '2026-09-02')"
                ),
                {"ip": ip, "ext": f"ORD-{RUN}-{ip}", "cust": f"P1 buyer {RUN}"},
            )
        # a payroll period + summary + employee so v_payroll_due has rows
        period = conn.execute(
            text(
                "INSERT INTO payroll_periods (start_date, end_date, status)"
                " VALUES ('2026-08-01', '2026-08-31', 'OPEN') RETURNING id"
            )
        ).scalar_one()
        for ip in (ip1, ip2):
            emp_ip = conn.execute(
                text(
                    "INSERT INTO employees (full_name, assigned_ip_id, payment_type,"
                    " base_salary) VALUES (:n, :ip, 'SALARY', 100000) RETURNING id"
                ),
                {"n": f"P1 payroll emp {RUN} {ip}", "ip": ip},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO payroll_summary (employee_id, payroll_period_id,"
                    " ip_id, piecework_total, base_salary, bonuses, deductions,"
                    " status)"
                    " VALUES (:e, :p, :ip, 0, 100000, 0, 0, 'CALCULATED')"
                ),
                {"e": emp_ip, "p": period, "ip": ip},
            )
    return {"ip1": ip1, "ip2": ip2, "admin": admin, "user": restricted}


def _login(client: TestClient, username: str, password: str) -> str:
    r = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


ENDPOINTS = [
    "/operations",
    "/operations/daily?date_from=2026-01-01&date_to=2026-12-31",
    "/orders",
    "/orders/statuses",
    "/reports/payroll-due",
]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_td016_01_requires_token(engine, applied_schema, endpoint):
    with _client() as client:
        r = client.get(endpoint)
    assert r.status_code == 401, f"{endpoint}: {r.text}"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_td016_02_admin_sees_rows(engine, applied_schema, two_ips, endpoint):
    with _client() as client:
        token = _login(client, two_ips["admin"], "P1-Admin-2026!")
        r = client.get(endpoint, headers=_auth(token))
    assert r.status_code == 200, f"{endpoint}: {r.text}"
    # /operations/daily is a series (no total); /orders/statuses is a picker.
    if not endpoint.startswith(("/orders/statuses", "/operations/daily")):
        assert r.json()["total"] >= 2, endpoint
    if endpoint.startswith("/operations/daily"):
        assert len(r.json()["items"]) >= 1, endpoint


def test_td016_02_restricted_cannot_query_foreign_ip(engine, applied_schema, two_ips):
    with _client() as client:
        token = _login(client, two_ips["user"], "P1-User-2026!")
        r = client.get(
            "/operations",
            headers=_auth(token),
            params={"ip_id": two_ips["ip2"]},
        )
    assert r.status_code == 403, r.text


def test_td016_02_restricted_sees_only_own_ip(engine, applied_schema, two_ips):
    with _client() as client:
        token = _login(client, two_ips["user"], "P1-User-2026!")
        r = client.get("/operations", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert {item["ip_id"] for item in body["items"]} == {two_ips["ip1"]}

    # daily series is isolated the same way (stricter than the old UI)
    r = client.get(
        "/operations/daily",
        headers=_auth(token),
        params={"date_from": "2026-01-01", "date_to": "2026-12-31"},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1
    assert r.json()["items"][0]["income"] == "1500.00"


def test_td016_02_restricted_cannot_create_order_for_foreign_ip(
    engine, applied_schema, two_ips
):
    """POST /orders keeps object-level authorization (control, not P1 scope)."""
    with _client() as client:
        token = _login(client, two_ips["user"], "P1-User-2026!")
        r = client.post(
            "/orders",
            headers=_auth(token),
            json={
                "ip_id": two_ips["ip2"],
                "source": "MANUAL",
                "order_date": "2026-09-01",
                "total_amount": "100.00",
                "kaspi_commission": "0.00",
                "delivery_fee": "0.00",
                "status": "NEW",
            },
        )
    assert r.status_code in (403, 404), r.text


def test_td016_03_pagination_and_total(engine, applied_schema, two_ips):
    with _client() as client:
        token = _login(client, two_ips["admin"], "P1-Admin-2026!")
        full = client.get(
            "/operations", headers=_auth(token), params={"limit": 1, "offset": 0}
        ).json()
        page2 = client.get(
            "/operations", headers=_auth(token), params={"limit": 1, "offset": 1}
        ).json()
        assert full["total"] >= 2
        assert len(full["items"]) == 1
        assert page2["items"][0]["id"] != full["items"][0]["id"]


def test_td016_03_orders_statuses_match_db_state_machine(
    engine, applied_schema, two_ips
):
    with _client() as client:
        token = _login(client, two_ips["admin"], "P1-Admin-2026!")
        r = client.get("/orders/statuses", headers=_auth(token))
    from app.models.orders import ORDER_STATUSES

    assert r.json()["statuses"] == list(ORDER_STATUSES)


def test_td016_05_amounts_are_strings(engine, applied_schema, two_ips):
    with _client() as client:
        token = _login(client, two_ips["admin"], "P1-Admin-2026!")
        r = client.get("/operations", headers=_auth(token))
    for item in r.json()["items"]:
        assert isinstance(item["amount"], str)
        assert "." in item["amount"]
