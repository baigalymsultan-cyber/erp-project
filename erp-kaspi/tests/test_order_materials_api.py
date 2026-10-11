"""P2-INV-002: Order materials API writer."""

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session


@pytest.fixture
def order_world(engine, applied_schema):
    import random

    bin_code = f"{random.randint(100000000000, 999999999999)}"[:12]
    with Session(engine) as session:
        ip_id = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('ORDER-MAT-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

        # Material with current_price 1500 (unique SKU per test run)
        sku = f"MAT-001-{random.randint(100000, 999999)}"
        mat_id = session.execute(
            text("""
                INSERT INTO materials (sku, name, unit, current_price)
                VALUES (:sku, 'Test Board', 'pcs', 1500) RETURNING id
            """),
            {"sku": sku},
        ).scalar_one()

        # Order
        order_id = session.execute(
            text("""
                INSERT INTO orders (ip_id, external_order_id, status, total_amount, source)
                VALUES (:ip, 'ORD-001', 'NEW', 10000, 'KASPI_SHOP') RETURNING id
            """),
            {"ip": ip_id},
        ).scalar_one()
        # P2-INV-001: the ledger gates consumption on on-hand stock —
        # seed a receipt so the world can actually consume material.
        session.execute(
            text("""
                INSERT INTO stock_movements
                    (ip_id, material_id, movement_date, quantity_delta, movement_type)
                VALUES (:ip, :m, CURRENT_DATE, 100, 'RECEIPT')
            """),
            {"ip": ip_id, "m": mat_id},
        )
        session.commit()

    return {"ip_id": ip_id, "mat_id": mat_id, "order_id": order_id}


def test_add_order_material_freezes_price(engine, applied_schema, order_world):
    """Service freezes material.current_price into price_at_time (§38)."""
    from app.schemas import OrderMaterialCreate
    from app.services import add_order_material

    w = order_world
    with Session(engine) as session:
        om = add_order_material(
            session,
            w["order_id"],
            OrderMaterialCreate(material_id=w["mat_id"], quantity=Decimal("2.5")),
        )
        session.commit()

        assert om.price_at_time == Decimal("1500.00")
        assert om.total_cost == Decimal("3750.00")  # 2.5 * 1500

        # Change material price — frozen price should NOT change
        session.execute(
            text("UPDATE materials SET current_price = 2000 WHERE id = :id"),
            {"id": w["mat_id"]},
        )
        session.commit()
        session.refresh(om)
        assert om.price_at_time == Decimal("1500.00")


def test_order_material_api(engine, applied_schema, order_world):
    """API endpoint creates order material with IP isolation."""
    from fastapi.testclient import TestClient

    from app.api import app
    from app.core.security import hash_password

    w = order_world
    with Session(engine) as session:
        pw_hash = hash_password("testpw123")
        user_id = session.execute(
            text("INSERT INTO users (username, password_hash) VALUES (:u, :h) RETURNING id"),
            {"u": f"ordermat-{w['ip_id']}", "h": pw_hash},
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
            json={"username": f"ordermat-{w['ip_id']}", "password": "testpw123"},
        )
        assert r.status_code == 200
        token = r.json()["access_token"]

        r = client.post(
            f"/orders/{w['order_id']}/materials",
            json={"material_id": w["mat_id"], "quantity": "3"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["price_at_time"] == "1500.00"
        assert body["total_cost"] == "4500.00"
