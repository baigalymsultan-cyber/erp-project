from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.api import app
from app.core.security import hash_password

RUN = uuid.uuid4().hex[:12]
PASSWORD = "FinOps-Test-2026!"
ISOLATED_DATE = date(2099, 1, 1)


def _create_ip_pair(engine: Engine) -> tuple[int, int]:
    suffix = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        ip_ids = []
        for index in (1, 2):
            bin_value = f"{(int(suffix, 16) + index) % 10**12:012d}"
            ip_ids.append(
                conn.execute(
                    text(
                        "INSERT INTO ip_entities (name, bin) VALUES (:name, :bin) "
                        "RETURNING id"
                    ),
                    {"name": f"FINOPS {suffix} {index}", "bin": bin_value},
                ).scalar_one()
            )
    return int(ip_ids[0]), int(ip_ids[1])


def _create_assigned_viewer(engine: Engine, ip_id: int) -> str:
    suffix = uuid.uuid4().hex[:8]
    username = f"finops-viewer-{RUN}-{suffix}"
    with engine.begin() as conn:
        employee_id = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
                "VALUES (:name, :ip_id, 'PIECEWORK') RETURNING id"
            ),
            {"name": f"FINOPS employee {RUN} {suffix}", "ip_id": ip_id},
        ).scalar_one()
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
                "SELECT :user_id, id FROM roles WHERE code = 'VIEWER'"
            ),
            {"user_id": user_id},
        )
    return username


def _seed_financial_operations(
    engine: Engine,
    ip1: int,
    ip2: int,
    operation_date: date = ISOLATED_DATE,
) -> None:
    with engine.begin() as conn:
        income_type = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_SHOP'")
        ).scalar_one()
        expense_type = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
        ).scalar_one()
        for ip_id, type_id, amount in (
            (ip1, income_type, "200.00"),
            (ip1, expense_type, "-50.00"),
            (ip2, income_type, "1000.00"),
            (ip2, expense_type, "-300.00"),
        ):
            conn.execute(
                text(
                    "INSERT INTO operations "
                    "(ip_id, type_id, operation_date, accrual_date, amount, status, comment) "
                    "VALUES (:ip_id, :type_id, :operation_date, :operation_date, :amount, 'POSTED', :comment)"
                ),
                {
                    "ip_id": ip_id,
                    "type_id": type_id,
                    "operation_date": operation_date,
                    "amount": amount,
                    "comment": f"FINOPS {RUN}",
                },
            )


def _login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_pnl_enforces_assigned_ip_for_aggregate_and_filter(
    engine: Engine, applied_schema, admin_headers
):
    ip1, ip2 = _create_ip_pair(engine)
    _seed_financial_operations(engine, ip1, ip2)
    username = _create_assigned_viewer(engine, ip1)
    period = {"period_start": ISOLATED_DATE.isoformat(), "period_end": ISOLATED_DATE.isoformat()}

    with TestClient(app) as client:
        viewer_headers = _login(client, username)
        scoped = client.get("/pnl", params=period, headers=viewer_headers)
        explicit_scoped = client.get(
            "/pnl", params={**period, "ip_id": ip1}, headers=viewer_headers
        )
        cross_ip = client.get("/pnl", params={**period, "ip_id": ip2}, headers=viewer_headers)
        global_result = client.get("/pnl", params=period, headers=admin_headers)

    assert scoped.status_code == 200, scoped.text
    assert {key: Decimal(value) for key, value in scoped.json().items()} == {
        "total_income": Decimal("200.00"),
        "total_expense": Decimal("50.00"),
        "net_profit": Decimal("150.00"),
        # GAP-001 layered keys: all expense here is EXPENSE_MATERIAL (direct).
        "revenue": Decimal("200.00"),
        "direct_cost": Decimal("50.00"),
        "gross_margin": Decimal("150.00"),
        "operating_expenses": Decimal("0.00"),
        "operating_result": Decimal("150.00"),
        "salary_unclassified": Decimal("0.00"),
        "other_income": Decimal("0.00"),
        "other_expenses": Decimal("0.00"),
        "net_result": Decimal("150.00"),
    }
    assert explicit_scoped.status_code == 200, explicit_scoped.text
    assert explicit_scoped.json() == scoped.json()
    assert cross_ip.status_code == 403, cross_ip.text
    assert global_result.status_code == 200, global_result.text
    assert {key: Decimal(value) for key, value in global_result.json().items()} == {
        "total_income": Decimal("1200.00"),
        "total_expense": Decimal("350.00"),
        "net_profit": Decimal("850.00"),
        "revenue": Decimal("1200.00"),
        "direct_cost": Decimal("350.00"),
        "gross_margin": Decimal("850.00"),
        "operating_expenses": Decimal("0.00"),
        "operating_result": Decimal("850.00"),
        "salary_unclassified": Decimal("0.00"),
        "other_income": Decimal("0.00"),
        "other_expenses": Decimal("0.00"),
        "net_result": Decimal("850.00"),
    }


def _create_director(engine: Engine) -> str:
    username = f"finops-director-{RUN}-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, is_active) "
                "VALUES (:username, :password_hash, TRUE) RETURNING id"
            ),
            {"username": username, "password_hash": hash_password(PASSWORD)},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT :user_id, id FROM roles WHERE code = 'DIRECTOR'"
            ),
            {"user_id": user_id},
        )
    return username


def test_pnl_director_keeps_global_scope(engine: Engine, applied_schema):
    ip1, ip2 = _create_ip_pair(engine)
    operation_date = ISOLATED_DATE + timedelta(days=1)
    _seed_financial_operations(engine, ip1, ip2, operation_date)
    username = _create_director(engine)
    period = {"period_start": operation_date.isoformat(), "period_end": operation_date.isoformat()}

    with TestClient(app) as client:
        headers = _login(client, username)
        global_result = client.get("/pnl", params=period, headers=headers)
        ip2_result = client.get(
            "/pnl", params={**period, "ip_id": ip2}, headers=headers
        )

    assert global_result.status_code == 200, global_result.text
    assert {key: Decimal(value) for key, value in global_result.json().items()} == {
        "total_income": Decimal("1200.00"),
        "total_expense": Decimal("350.00"),
        "net_profit": Decimal("850.00"),
        "revenue": Decimal("1200.00"),
        "direct_cost": Decimal("350.00"),
        "gross_margin": Decimal("850.00"),
        "operating_expenses": Decimal("0.00"),
        "operating_result": Decimal("850.00"),
        "salary_unclassified": Decimal("0.00"),
        "other_income": Decimal("0.00"),
        "other_expenses": Decimal("0.00"),
        "net_result": Decimal("850.00"),
    }
    assert ip2_result.status_code == 200, ip2_result.text
    assert {key: Decimal(value) for key, value in ip2_result.json().items()} == {
        "total_income": Decimal("1000.00"),
        "total_expense": Decimal("300.00"),
        "net_profit": Decimal("700.00"),
        "revenue": Decimal("1000.00"),
        "direct_cost": Decimal("300.00"),
        "gross_margin": Decimal("700.00"),
        "operating_expenses": Decimal("0.00"),
        "operating_result": Decimal("700.00"),
        "salary_unclassified": Decimal("0.00"),
        "other_income": Decimal("0.00"),
        "other_expenses": Decimal("0.00"),
        "net_result": Decimal("700.00"),
    }


def test_pnl_empty_result_date_filter_and_authentication(engine: Engine, applied_schema):
    ip1, ip2 = _create_ip_pair(engine)
    operation_date = ISOLATED_DATE + timedelta(days=2)
    _seed_financial_operations(engine, ip1, ip2, operation_date)
    username = _create_assigned_viewer(engine, ip1)
    empty_date = operation_date + timedelta(days=1)
    empty_period = {"period_start": empty_date.isoformat(), "period_end": empty_date.isoformat()}

    with TestClient(app) as client:
        headers = _login(client, username)
        empty = client.get("/pnl", params=empty_period, headers=headers)
        unauthorized = client.get("/pnl", params=empty_period)

    assert empty.status_code == 200, empty.text
    assert {key: Decimal(value) for key, value in empty.json().items()} == {
        "total_income": Decimal("0.00"),
        "total_expense": Decimal("0.00"),
        "net_profit": Decimal("0.00"),
        "revenue": Decimal("0.00"),
        "direct_cost": Decimal("0.00"),
        "gross_margin": Decimal("0.00"),
        "operating_expenses": Decimal("0.00"),
        "operating_result": Decimal("0.00"),
        "salary_unclassified": Decimal("0.00"),
        "other_income": Decimal("0.00"),
        "other_expenses": Decimal("0.00"),
        "net_result": Decimal("0.00"),
    }
    assert unauthorized.status_code == 401, unauthorized.text


def test_operations_summary_uses_posted_status_and_matches_filtered_details(
    engine: Engine, applied_schema, admin_headers
):
    ip_a, ip_b = _create_ip_pair(engine)
    username = _create_assigned_viewer(engine, ip_a)
    operation_date = ISOLATED_DATE + timedelta(days=10)
    comment = f"FINOPS summary {RUN}"
    with engine.begin() as conn:
        type_ids = dict(
            conn.execute(
                text(
                    "SELECT code, id FROM operation_types "
                    "WHERE code IN ('SALE_KASPI_SHOP', 'EXPENSE_MATERIAL')"
                )
            ).all()
        )
        for ip_id, code, amount, status_value in (
            (ip_a, "SALE_KASPI_SHOP", "200.00", "POSTED"),
            (ip_a, "EXPENSE_MATERIAL", "-50.00", "POSTED"),
            (ip_a, "SALE_KASPI_SHOP", "900.00", "DRAFT"),
            (ip_a, "SALE_KASPI_SHOP", "80.00", "REVERSED"),
            (ip_a, "EXPENSE_MATERIAL", "-30.00", "CORRECTED"),
            (ip_b, "SALE_KASPI_SHOP", "1000.00", "POSTED"),
        ):
            conn.execute(
                text(
                    "INSERT INTO operations "
                    "(ip_id, type_id, operation_date, accrual_date, amount, status, comment) "
                    "VALUES (:ip_id, :type_id, :operation_date, :operation_date, :amount, :status, :comment)"
                ),
                {
                    "ip_id": ip_id,
                    "type_id": type_ids[code],
                    "operation_date": operation_date,
                    "amount": amount,
                    "status": status_value,
                    "comment": comment,
                },
            )

    with engine.begin() as conn:
        source_account_id = conn.execute(
            text(
                "INSERT INTO accounts (ip_id, type, name) "
                "VALUES (:ip_id, 'OTHER', :name) RETURNING id"
            ),
            {"ip_id": ip_a, "name": f"FINOPS source {RUN}"},
        ).scalar_one()
        destination_account_id = conn.execute(
            text(
                "INSERT INTO accounts (ip_id, type, name) "
                "VALUES (:ip_id, 'OTHER', :name) RETURNING id"
            ),
            {"ip_id": ip_b, "name": f"FINOPS destination {RUN}"},
        ).scalar_one()

    period = {
        "period_start": operation_date.isoformat(),
        "period_end": operation_date.isoformat(),
        "q": comment,
    }
    with TestClient(app) as client:
        transfer = client.post(
            "/transfers",
            json={
                "from_ip_id": ip_a,
                "to_ip_id": ip_b,
                "from_account_id": source_account_id,
                "to_account_id": destination_account_id,
                "amount": "70.00",
                "transfer_date": operation_date.isoformat(),
                "comment": comment,
            },
            headers={**admin_headers, "Idempotency-Key": f"finops-summary-{RUN}"},
        )
        assert transfer.status_code == 201, transfer.text
        viewer_headers = _login(client, username)
        summary = client.get("/operations/summary", params=period, headers=viewer_headers)
        detail = client.get("/operations", params=period, headers=viewer_headers)
        income_summary = client.get(
            "/operations/summary", params={**period, "kind": "INCOME"}, headers=viewer_headers
        )
        income_detail = client.get(
            "/operations", params={**period, "kind": "INCOME"}, headers=viewer_headers
        )
        expense_summary = client.get(
            "/operations/summary", params={**period, "kind": "EXPENSE"}, headers=viewer_headers
        )
        expense_detail = client.get(
            "/operations", params={**period, "kind": "EXPENSE"}, headers=viewer_headers
        )
        empty = client.get(
            "/operations/summary",
            params={**period, "period_start": (operation_date + timedelta(days=1)).isoformat(),
                    "period_end": (operation_date + timedelta(days=1)).isoformat()},
            headers=viewer_headers,
        )
        foreign_ip = client.get(
            "/operations/summary", params={**period, "ip_id": ip_b}, headers=viewer_headers
        )
        global_result = client.get("/operations/summary", params=period, headers=admin_headers)
        unauthorized = client.get("/operations/summary", params=period)

    assert summary.status_code == 200, summary.text
    assert detail.status_code == 200, detail.text
    body = summary.json()
    items = detail.json()["items"]
    assert body["income"] == "200.00"
    assert body["expense"] == "50.00"
    assert body["transfer_count"] == 1
    assert body["posted_count"] == 3
    assert body["count"] == detail.json()["total"] == 6
    assert body["status_counts"] == {
        "POSTED": 3,
        "DRAFT": 1,
        "REVERSED": 1,
        "CORRECTED": 1,
    }
    posted_income = sum(
        (Decimal(item["amount"]) for item in items
         if item["kind"] == "INCOME" and item["status"] == "POSTED"),
        Decimal("0.00"),
    )
    posted_expense = sum(
        (-Decimal(item["amount"]) for item in items
         if item["kind"] == "EXPENSE" and item["status"] == "POSTED"),
        Decimal("0.00"),
    )
    assert posted_income == Decimal(body["income"])
    assert posted_expense == Decimal(body["expense"])

    assert income_summary.status_code == income_detail.status_code == 200
    assert income_summary.json()["income"] == "200.00"
    assert income_summary.json()["expense"] == "0.00"
    assert income_summary.json()["count"] == income_detail.json()["total"] == 3
    assert expense_summary.status_code == expense_detail.status_code == 200
    assert expense_summary.json()["income"] == "0.00"
    assert expense_summary.json()["expense"] == "50.00"
    assert expense_summary.json()["count"] == expense_detail.json()["total"] == 2
    assert empty.status_code == 200, empty.text
    assert empty.json() == {
        "income": "0.00",
        "expense": "0.00",
        "count": 0,
        "posted_count": 0,
        "transfer_count": 0,
        "status_counts": {"POSTED": 0, "DRAFT": 0, "REVERSED": 0, "CORRECTED": 0},
    }
    assert foreign_ip.status_code == 403, foreign_ip.text
    assert global_result.status_code == 200, global_result.text
    assert Decimal(global_result.json()["income"]) == Decimal("1200.00")
    assert Decimal(global_result.json()["expense"]) == Decimal("50.00")
    assert global_result.json()["count"] == 8
    assert global_result.json()["transfer_count"] == 2
    assert unauthorized.status_code == 401, unauthorized.text
