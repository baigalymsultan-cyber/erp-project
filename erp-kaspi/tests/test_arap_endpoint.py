"""GAP-003: AR/AP counterparty balances endpoint."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session


@pytest.fixture
def arap_world(engine, applied_schema):
    import random

    bin_code = f"{random.randint(100000000000, 999999999999)}"[:12]
    with Session(engine) as session:
        ip_id = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('ARAP-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

        cp_id = session.execute(
            text("INSERT INTO counterparties (name) VALUES ('Test Supplier') RETURNING id"),
        ).scalar_one()

        exp_type = session.execute(
            text("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
        ).scalar_one()

        # Accrued expense 500000 (AP: we owe them)
        session.execute(
            text("""
                INSERT INTO operations
                (ip_id, type_id, counterparty_id, operation_date, accrual_date, amount, status)
                VALUES (:ip, :t, :cp, '2026-01-15', '2026-01-15', -500000, 'POSTED')
            """),
            {"ip": ip_id, "t": exp_type, "cp": cp_id},
        )
        # Partial payment 200000 (accrual_date NULL = pure cash)
        session.execute(
            text("""
                INSERT INTO operations
                (ip_id, type_id, counterparty_id, operation_date, accrual_date, amount, status)
                VALUES (:ip, :t, :cp, '2026-02-10', NULL, -200000, 'POSTED')
            """),
            {"ip": ip_id, "t": exp_type, "cp": cp_id},
        )
        session.commit()

    return {"ip_id": ip_id, "cp_id": cp_id}


def test_counterparty_balances_service(engine, applied_schema, arap_world):
    """Service returns net position per counterparty."""
    from sqlalchemy.orm import Session

    from app.services import counterparty_balances

    w = arap_world
    with Session(engine) as session:
        balances = counterparty_balances(session, [w["ip_id"]])

    assert len(balances) == 1
    b = balances[0]
    assert b["counterparty_id"] == w["cp_id"]
    assert b["counterparty_name"] == "Test Supplier"
    # total_accrued = -500000, total_settled = -200000
    # balance = accrued - settled = -500000 - (-200000) = -300000 (AP)
    assert float(b["balance"]) == -300000.0


def test_counterparty_balances_api(engine, applied_schema, arap_world):
    """API endpoint returns AR/AP with IP isolation."""
    from fastapi.testclient import TestClient

    from app.api import app
    from app.core.security import hash_password

    w = arap_world
    with Session(engine) as session:
        # Create admin user
        pw_hash = hash_password("testpw123")
        user_id = session.execute(
            text("""
                INSERT INTO users (username, password_hash)
                VALUES (:u, :h) RETURNING id
            """),
            {"u": f"arap-admin-{w['ip_id']}", "h": pw_hash},
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
        # Login
        r = client.post(
            "/auth/login",
            json={"username": f"arap-admin-{w['ip_id']}", "password": "testpw123"},
        )
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]

        # Get balances
        r = client.get(
            "/reports/counterparty-balances",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] >= 1
        # Find our counterparty
        items = [i for i in body["items"] if i["counterparty_id"] == w["cp_id"]]
        assert len(items) == 1
        assert items[0]["counterparty_name"] == "Test Supplier"
