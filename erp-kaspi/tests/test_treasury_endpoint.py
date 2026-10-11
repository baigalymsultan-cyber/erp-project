"""GAP-002: Per-account cash balances."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session


@pytest.fixture
def treasury_world(engine, applied_schema):
    import random

    bin_code = f"{random.randint(100000000000, 999999999999)}"[:12]
    with Session(engine) as session:
        ip_id = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('TREASURY-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

        # Create two accounts
        acc1 = session.execute(
            text("""
                INSERT INTO accounts (ip_id, type, name)
                VALUES (:ip, 'KASPI_PAY', 'Kaspi Pay') RETURNING id
            """),
            {"ip": ip_id},
        ).scalar_one()
        acc2 = session.execute(
            text("""
                INSERT INTO accounts (ip_id, type, name)
                VALUES (:ip, 'CASH', 'Cash') RETURNING id
            """),
            {"ip": ip_id},
        ).scalar_one()

        inc_type = session.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_SHOP'")
        ).scalar_one()

        # Income 100000 to Kaspi Pay
        session.execute(
            text("""
                INSERT INTO operations
                (ip_id, type_id, account_id, operation_date, amount, status)
                VALUES (:ip, :t, :acc, '2026-01-15', 100000, 'POSTED')
            """),
            {"ip": ip_id, "t": inc_type, "acc": acc1},
        )
        # Income 50000 to Cash
        session.execute(
            text("""
                INSERT INTO operations
                (ip_id, type_id, account_id, operation_date, amount, status)
                VALUES (:ip, :t, :acc, '2026-01-16', 50000, 'POSTED')
            """),
            {"ip": ip_id, "t": inc_type, "acc": acc2},
        )
        session.commit()

    return {"ip_id": ip_id, "acc1": acc1, "acc2": acc2}


def test_account_balances_service(engine, applied_schema, treasury_world):
    from app.services import account_balances

    w = treasury_world
    with Session(engine) as session:
        balances = account_balances(session, [w["ip_id"]])

    assert len(balances) == 2
    by_id = {b["account_id"]: b for b in balances}
    assert float(by_id[w["acc1"]]["balance"]) == 100000.0
    assert float(by_id[w["acc2"]]["balance"]) == 50000.0
    assert by_id[w["acc1"]]["type"] == "KASPI_PAY"


def test_account_balances_api(engine, applied_schema, treasury_world):
    from fastapi.testclient import TestClient

    from app.api import app
    from app.core.security import hash_password

    w = treasury_world
    with Session(engine) as session:
        pw_hash = hash_password("testpw123")
        user_id = session.execute(
            text("INSERT INTO users (username, password_hash) VALUES (:u, :h) RETURNING id"),
            {"u": f"treasury-{w['ip_id']}", "h": pw_hash},
        ).scalar_one()
        role_id = session.execute(
            text("SELECT id FROM roles WHERE code = 'ADMIN'")
        ).scalar_one()
        session.execute(
            text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
            {"u": user_id, "r": role_id},
        )
        session.commit()

    with TestClient(app) as client:
        r = client.post(
            "/auth/login",
            json={"username": f"treasury-{w['ip_id']}", "password": "testpw123"},
        )
        assert r.status_code == 200
        token = r.json()["access_token"]

        r = client.get(
            "/reports/account-balances",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] >= 2
