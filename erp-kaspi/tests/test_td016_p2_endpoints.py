"""TD-016 P2 — read-only reference/report endpoints (E5–E13).

REQ-TD016-01: 401 without token on every endpoint.
REQ-TD016-02: server-side ИП isolation — a restricted principal cannot widen
the filter nor see another ИП's rows (BOLA / cross-IP leak negative tests).
REQ-TD016-03: server paging with total where a list is returned.
REQ-TD016-05: amounts are Decimal-safe strings.
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
BIN_BASE = 907100000000

ENDPOINTS = [
    "/reports/ip-summary",
    "/reports/kaspi-reconciliation",
    "/reports/order-costing",
    "/reports/work-entries",
    "/products",
    "/materials",
    "/accounts",
    "/kaspi/sync-log",
    "/import-batches",
]


def _client() -> TestClient:
    return TestClient(app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient, username: str, password: str) -> str:
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    from app.core.security import reset_auth_rate_limit

    reset_auth_rate_limit()
    yield


@pytest.fixture(scope="module")
def world(engine):
    """Two ИП, products/materials, accounts on both ИП, a Kaspi order+payout,
    sync log rows, import batch, an ADMIN and a USER bound only to ИП-1."""
    with engine.begin() as conn:
        ip1 = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": f"P2A-{RUN}", "b": str(BIN_BASE + 1)},
        ).scalar_one()
        ip2 = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": f"P2B-{RUN}", "b": str(BIN_BASE + 2)},
        ).scalar_one()

        for sku, name in (("p2-sku-1", "P2 Product 1"), ("p2-sku-2", "P2 Product 2")):
            conn.execute(
                text("INSERT INTO products (sku, name, selling_price) VALUES (:s, :n, 150.50)"),
                {"s": f"{sku}-{RUN[:4]}", "n": name},
            )
        conn.execute(
            text(
                "INSERT INTO materials (sku, name, unit, current_price) "
                "VALUES (:s, :n, 'кг', 42.00)"
            ),
            {"s": f"p2-mat-{RUN[:4]}", "n": "P2 Material"},
        )

        acc1 = conn.execute(
            text(
                "INSERT INTO accounts (ip_id, type, name) "
                "VALUES (:ip, 'KASPI_PAY', :n) RETURNING id"
            ),
            {"ip": ip1, "n": f"P2 Acc 1 {RUN}"},
        ).scalar_one()
        acc2 = conn.execute(
            text(
                "INSERT INTO accounts (ip_id, type, name) "
                "VALUES (:ip, 'KASPI_PAY', :n) RETURNING id"
            ),
            {"ip": ip2, "n": f"P2 Acc 2 {RUN}"},
        ).scalar_one()

        ot_income = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_SHOP'")
        ).scalar_one()
        op1 = conn.execute(
            text(
                "INSERT INTO operations (ip_id, operation_date, accrual_date, type_id, amount, status, "
                "account_id, comment) "
                "VALUES (:ip, CURRENT_DATE, CURRENT_DATE, :t, 1000.00, 'POSTED', :acc, 'p2 seeded op 1') "
                "RETURNING id"
            ),
            {"ip": ip1, "t": ot_income, "acc": acc1},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO operations (ip_id, operation_date, accrual_date, type_id, amount, status, "
                "account_id, comment) "
                "VALUES (:ip, CURRENT_DATE, CURRENT_DATE, :t, 2000.00, 'POSTED', :acc, 'p2 seeded op 2') "
                "RETURNING id"
            ),
            {"ip": ip2, "t": ot_income, "acc": acc2},
        )

        order1 = conn.execute(
            text(
                "INSERT INTO orders (ip_id, external_order_id, order_date, status, source, "
                "total_amount, kaspi_commission, delivery_fee) "
                "VALUES (:ip, :ext, CURRENT_DATE, 'COMPLETED', 'KASPI_SHOP', 10000.00, "
                "500.00, 300.00) RETURNING id"
            ),
            {"ip": ip1, "ext": f"P2-ORD-{RUN}"},
        ).scalar_one()

        payout1 = conn.execute(
            text(
                "INSERT INTO kaspi_payouts (ip_id, external_payout_id, payout_date, status, "
                "gross_amount, net_amount, operation_id) "
                "VALUES (:ip, :ext, CURRENT_DATE, 'RECONCILED', 10000.00, 9200.00, :op) "
                "RETURNING id"
            ),
            {"ip": ip1, "ext": f"P2-PO-{RUN}", "op": op1},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO kaspi_payout_items (payout_id, item_type, gross_amount, "
                "commission_amount, delivery_amount, refund_amount, net_amount, match_status, "
                "order_id) "
                "VALUES (:p, 'SALE', 10000.00, 500.00, 300.00, 0, 9200.00, 'MATCHED', :o)"
            ),
            {"p": payout1, "o": order1},
        )

        conn.execute(
            text(
                "INSERT INTO sync_log (ip_id, api_type, status, records_fetched, "
                "records_inserted, records_updated, errors_count) "
                "VALUES (:ip, 'SHOP', 'SUCCESS', 5, 3, 1, 0)"
            ),
            {"ip": ip1},
        )
        conn.execute(
            text(
                "INSERT INTO import_batches (source_type, source_name, status, rows_total, "
                "rows_inserted, rows_skipped) "
                "VALUES ('EXCEL', 'p2-import.xlsx', 'SUCCESS', 10, 8, 2)"
            )
        )

        admin = f"p2-admin-{RUN}"
        user = f"p2-user-{RUN}"
        pw = "P2-Pass-2026!"
        uid_admin = conn.execute(
            text("INSERT INTO users (username, password_hash) VALUES (:u, :h) RETURNING id"),
            {"u": admin, "h": hash_password(pw)},
        ).scalar_one()
        uid_user = conn.execute(
            text("INSERT INTO users (username, password_hash) VALUES (:u, :h) RETURNING id"),
            {"u": user, "h": hash_password(pw)},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT :uid, id FROM roles WHERE code = 'ADMIN'"
            ),
            {"uid": uid_admin},
        )
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT :uid, id FROM roles WHERE code = 'VIEWER'"
            ),
            {"uid": uid_user},
        )
        emp_user = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
                "VALUES (:n, :ip, 'PIECEWORK') RETURNING id"
            ),
            {"n": f"P2 Emp {RUN}", "ip": ip1},
        ).scalar_one()
        conn.execute(
            text("UPDATE users SET employee_id = :e WHERE id = :u"),
            {"e": emp_user, "u": uid_user},
        )

        wot1 = conn.execute(
            text("SELECT id FROM work_operation_types WHERE code = 'SAWING'")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO work_entries (employee_id, work_operation_type_id, ip_id, "
                "entry_date, quantity, rate_at_time, status) "
                "VALUES (:e, :w, :ip, CURRENT_DATE, 10, 50.00, 'DRAFT')"
            ),
            {"e": emp_user, "w": wot1, "ip": ip1},
        )

    return {"ip1": ip1, "ip2": ip2, "admin": admin, "user": user, "pw": pw}


@pytest.mark.usefixtures("_fresh_rate_limit")
@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_p2_01_requires_token(engine, applied_schema, endpoint):
    with _client() as client:
        r = client.get(endpoint)
    assert r.status_code == 401, f"{endpoint}: {r.text}"


@pytest.mark.usefixtures("_fresh_rate_limit")
@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_p2_02_admin_sees_rows(engine, applied_schema, world, endpoint):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        r = client.get(endpoint, headers=_auth(token))
    assert r.status_code == 200, f"{endpoint}: {r.text}"
    body = r.json()
    assert body["total"] >= 1, f"{endpoint}: {body}"


@pytest.mark.parametrize(
    "endpoint",
    [
        "/reports/ip-summary",
        "/reports/kaspi-reconciliation",
        "/reports/order-costing",
        "/reports/work-entries",
        "/accounts",
        "/kaspi/sync-log",
    ],
)
@pytest.mark.usefixtures("_fresh_rate_limit")
def test_p2_03_restricted_sees_only_own_ip(engine, applied_schema, world, endpoint):
    """BOLA/cross-IP: the USER is bound to ИП-1 only and must never see ИП-2 rows."""
    with _client() as client:
        token = _login(client, world["user"], world["pw"])
        r = client.get(endpoint, headers=_auth(token))
    assert r.status_code == 200, f"{endpoint}: {r.text}"
    body = r.json()
    assert body["total"] >= 1, f"{endpoint}: {body}"
    for item in body["items"]:
        assert item["ip_id"] == world["ip1"], f"{endpoint}: leaked {item}"


def test_p2_04_restricted_cannot_query_foreign_ip(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["user"], world["pw"])
        for endpoint in ("/accounts", "/kaspi/sync-log", "/reports/order-costing"):
            r = client.get(
                endpoint, headers=_auth(token), params={"ip_id": world["ip2"]}
            )
            assert r.status_code in (200, 403), f"{endpoint}: {r.text}"
            if r.status_code == 200:
                assert r.json()["total"] == 0, f"{endpoint}: leaked cross-IP rows"


def test_p2_05_pagination_and_total(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        r1 = client.get("/products", headers=_auth(token), params={"limit": 1, "offset": 0})
        r2 = client.get("/products", headers=_auth(token), params={"limit": 1, "offset": 1})
        assert r1.status_code == 200 and r2.status_code == 200
        b1, b2 = r1.json(), r2.json()
        assert b1["total"] >= 2
        assert len(b1["items"]) == 1
        assert b1["items"][0]["id"] != b2["items"][0]["id"]


def test_p2_06_pagination_caps(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        r = client.get("/products", headers=_auth(token), params={"limit": 500})
    assert r.status_code == 422, r.text


def test_p2_07_amounts_are_strings(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        r = client.get("/reports/ip-summary", headers=_auth(token))
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert isinstance(item["total_income"], str)
            assert "." in item["total_income"]
        r = client.get("/reports/order-costing", headers=_auth(token))
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert isinstance(item["revenue"], str)


def test_p2_08_viewer_forbidden_on_import_batches(engine, applied_schema, world):
    """import-batches is an ACCOUNTANT+ report: VIEWER must be denied (403)."""
    with _client() as client:
        token = _login(client, world["user"], world["pw"])
        r = client.get("/import-batches", headers=_auth(token))
    assert r.status_code == 403, r.text


def test_p2_09_ip_summary_shape(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        r = client.get(
            "/reports/ip-summary", headers=_auth(token), params={"ip_id": world["ip1"]}
        )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    row = items[0]
    assert row["ip_id"] == world["ip1"]
    assert row["total_income"] == "1000.00"
    assert row["revenue_kaspi_shop"] == "1000.00"
    assert row["operations_count"] == 1


def test_p2_10_order_costing_shape(engine, applied_schema, world):
    with _client() as client:
        token = _login(client, world["admin"], world["pw"])
        r = client.get(
            "/reports/order-costing", headers=_auth(token), params={"ip_id": world["ip1"]}
        )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    row = items[0]
    assert row["revenue"] == "10000.00"
    assert row["kaspi_commission"] == "500.00"
