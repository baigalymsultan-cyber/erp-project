"""Tests for G-C2 production output + G-C4 order_direct_cost alignment.

Real PostgreSQL (no mocks): the database is the enforcement layer.
Covers:
  * G-C4: order_direct_cost() == v_order_costing with DRAFT/CONFIRMED/PAID entries
  * production output increases on-hand (v_finished_goods_stock)
  * COMPLETED order delivery decreases on-hand
  * DI-001: cross-IP output blocked (trigger, raw SQL — the service derives
    the IP from the order so it cannot produce a mismatch itself)
  * DI-005 STRONG: UPDATE/DELETE blocked without erp_migrator + GUC
  * naive produced_at rejected; make-to-stock requires ip_id
  * API: POST /production/output (SHOP_MANAGER+, order-bound + make-to-stock),
    GET /production/finished-goods (VIEWER, IP-scoped)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api import app
from app.core.security import hash_password
from app.services import order_direct_cost
from app.services.inventory import record_production_output

PASSWORD = "test-only-password"


def _world(session, suffix: str | None = None) -> dict[str, int]:
    """Two IPs, one product, one order per IP, one SHOP_MANAGER on ip_a."""
    sfx = suffix or uuid.uuid4().hex[:8]
    ip_a = session.execute(
        text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
        {"n": f"PROD ip-a {sfx}", "b": f"{780000000000 + int(sfx, 16) % 10**9:012d}"},
    ).scalar_one()
    ip_b = session.execute(
        text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
        {"n": f"PROD ip-b {sfx}", "b": f"{781000000000 + int(sfx, 16) % 10**9:012d}"},
    ).scalar_one()
    product = session.execute(
        text("INSERT INTO products (sku, name, selling_price) VALUES (:s, :n, 50000) RETURNING id"),
        {"s": f"PROD-{sfx}", "n": f"Test chair {sfx}"},
    ).scalar_one()
    order_a = session.execute(
        text(
            "INSERT INTO orders (ip_id, status, total_amount, source) "
            "VALUES (:ip, 'APPROVED', 100000, 'MANUAL') RETURNING id"
        ),
        {"ip": ip_a},
    ).scalar_one()
    order_b = session.execute(
        text(
            "INSERT INTO orders (ip_id, status, total_amount, source) "
            "VALUES (:ip, 'APPROVED', 200000, 'MANUAL') RETURNING id"
        ),
        {"ip": ip_b},
    ).scalar_one()
    emp = session.execute(
        text(
            "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary) "
            "VALUES (:n, :ip, 'SALARY', 0) RETURNING id"
        ),
        {"n": f"PROD mgr {sfx}", "ip": ip_a},
    ).scalar_one()
    username = f"prod-mgr-{sfx}"
    user_id = session.execute(
        text(
            "INSERT INTO users (username, password_hash, employee_id, is_active) "
            "VALUES (:u, :h, :e, TRUE) RETURNING id"
        ),
        {"u": username, "h": hash_password(PASSWORD), "e": emp},
    ).scalar_one()
    session.execute(
        text(
            "INSERT INTO user_roles (user_id, role_id) "
            "SELECT :u, id FROM roles WHERE code = 'SHOP_MANAGER'"
        ),
        {"u": user_id},
    )
    return {
        "ip_a": ip_a,
        "ip_b": ip_b,
        "product": product,
        "order_a": order_a,
        "order_b": order_b,
        "manager": username,
    }


def _login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _on_hand(session, ip_id: int, product_id: int) -> Decimal:
    return Decimal(
        session.execute(
            text(
                "SELECT on_hand FROM v_finished_goods_stock WHERE ip_id = :ip AND product_id = :p"
            ),
            {"ip": ip_id, "p": product_id},
        ).scalar_one_or_none()
        or 0
    )


# --------------------------------------------------------------------------- #
# G-C4: order_direct_cost() aligns with v_order_costing
# --------------------------------------------------------------------------- #
def test_direct_cost_matches_view_with_draft_confirmed_paid(session: Session) -> None:
    w = _world(session)
    wot = session.execute(
        text(
            "INSERT INTO work_operation_types (code, name, unit, calc_mode) "
            "VALUES (:c, 'test op', 'pcs', 'per_unit') "
            "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        ),
        {"c": f"PRODCOST-{uuid.uuid4().hex[:8]}"},
    ).scalar_one()
    emp = session.execute(
        text(
            "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
            "VALUES ('cost worker', :ip, 'PIECEWORK') RETURNING id"
        ),
        {"ip": w["ip_a"]},
    ).scalar_one()
    # DRAFT (10*1000=10000) must be EXCLUDED; CONFIRMED (5*1000=5000) and
    # PAID (2*1000=2000) must be INCLUDED → labour = 7000.
    # PAID entries require a payroll period (chk_work_entries_paid_period).
    period = session.execute(
        text(
            "INSERT INTO payroll_periods (start_date, end_date, status) "
            "VALUES (CURRENT_DATE - 30, CURRENT_DATE, 'OPEN') RETURNING id"
        )
    ).scalar_one()
    for st, qty in (("DRAFT", 10), ("CONFIRMED", 5), ("PAID", 2)):
        session.execute(
            text(
                "INSERT INTO work_entries (ip_id, employee_id, work_operation_type_id, "
                "order_id, entry_date, quantity, status, rate_at_time, payroll_period_id) "
                "VALUES (:ip, :e, :w, :o, CURRENT_DATE, :q, :s, 1000, :p)"
            ),
            {
                "ip": w["ip_a"],
                "e": emp,
                "w": wot,
                "o": w["order_a"],
                "q": qty,
                "s": st,
                "p": period if st == "PAID" else None,
            },
        )
    session.commit()

    got = order_direct_cost(session, w["order_a"])
    assert got == {
        "material_cost": Decimal("0.00"),
        "labor_cost": Decimal("7000.00"),
        "total_direct_cost": Decimal("7000.00"),
    }
    row = session.execute(
        text(
            "SELECT material_cost, labor_cost, total_direct_cost FROM v_order_costing "
            "WHERE order_id = :o"
        ),
        {"o": w["order_a"]},
    ).one()
    assert Decimal(row.material_cost) == got["material_cost"]
    assert Decimal(row.labor_cost) == got["labor_cost"]
    assert Decimal(row.total_direct_cost) == got["total_direct_cost"]


# --------------------------------------------------------------------------- #
# Production output → on-hand
# --------------------------------------------------------------------------- #
def test_output_increases_on_hand(session: Session) -> None:
    w = _world(session)
    oid = record_production_output(
        session, ip_id=w["ip_a"], product_id=w["product"], quantity=Decimal("7")
    )
    assert oid > 0
    assert _on_hand(session, w["ip_a"], w["product"]) == Decimal("7")
    # Second output accumulates
    record_production_output(
        session, ip_id=w["ip_a"], product_id=w["product"], quantity=Decimal("3")
    )
    assert _on_hand(session, w["ip_a"], w["product"]) == Decimal("10")
    # Other IP untouched
    assert _on_hand(session, w["ip_b"], w["product"]) == Decimal("0")


def test_order_bound_output_derives_ip_from_order(session: Session) -> None:
    w = _world(session)
    # Caller passes a bogus ip_id — the order's IP wins (DI-001 safe).
    oid = record_production_output(
        session,
        ip_id=w["ip_b"],  # wrong on purpose
        product_id=w["product"],
        quantity=Decimal("2"),
        order_id=w["order_a"],
    )
    row = session.execute(
        text("SELECT ip_id, order_id FROM production_output WHERE id = :i"), {"i": oid}
    ).one()
    assert row.ip_id == w["ip_a"]
    assert row.order_id == w["order_a"]


def test_completed_order_delivery_decreases_on_hand(session: Session) -> None:
    w = _world(session)
    record_production_output(
        session, ip_id=w["ip_a"], product_id=w["product"], quantity=Decimal("10")
    )
    session.execute(
        text(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price) "
            "VALUES (:o, :p, 4, 50000)"
        ),
        {"o": w["order_a"], "p": w["product"]},
    )
    # Not COMPLETED yet → delivered_qty stays 0
    assert _on_hand(session, w["ip_a"], w["product"]) == Decimal("10")
    session.execute(
        text("UPDATE orders SET status = 'COMPLETED', completed_at = order_date WHERE id = :o"),
        {"o": w["order_a"]},
    )
    session.commit()
    row = session.execute(
        text(
            "SELECT produced_qty, delivered_qty, on_hand FROM v_finished_goods_stock "
            "WHERE ip_id = :ip AND product_id = :p"
        ),
        {"ip": w["ip_a"], "p": w["product"]},
    ).one()
    assert Decimal(row.produced_qty) == Decimal("10")
    assert Decimal(row.delivered_qty) == Decimal("4")
    assert Decimal(row.on_hand) == Decimal("6")


# --------------------------------------------------------------------------- #
# DI-001 / DI-005 / validation
# --------------------------------------------------------------------------- #
def test_cross_ip_output_blocked_by_trigger(session: Session) -> None:
    w = _world(session)
    with pytest.raises(DBAPIError, match="DI-001"), session.begin_nested():
        session.execute(
            text(
                "INSERT INTO production_output (ip_id, order_id, product_id, quantity) "
                "VALUES (:ip, :o, :p, 1)"
            ),
            {"ip": w["ip_b"], "o": w["order_a"], "p": w["product"]},
        )
        session.flush()


def test_update_and_delete_blocked(session: Session) -> None:
    w = _world(session)
    oid = record_production_output(
        session, ip_id=w["ip_a"], product_id=w["product"], quantity=Decimal("5")
    )
    session.flush()
    # Savepoints: the failed flush aborts only the savepoint, the world's
    # outer transaction stays usable for the next attempt.
    with pytest.raises(DBAPIError, match="immutable"), session.begin_nested():
        session.execute(
            text("UPDATE production_output SET quantity = 99 WHERE id = :i"), {"i": oid}
        )
        session.flush()
    with pytest.raises(DBAPIError, match="immutable"), session.begin_nested():
        session.execute(text("DELETE FROM production_output WHERE id = :i"), {"i": oid})
        session.flush()
    # The row is still there — nothing was mutated.
    assert _on_hand(session, w["ip_a"], w["product"]) == Decimal("5")


def test_naive_produced_at_rejected(session: Session) -> None:
    w = _world(session)
    with pytest.raises(ValueError, match="timezone-aware"):
        record_production_output(
            session,
            ip_id=w["ip_a"],
            product_id=w["product"],
            quantity=Decimal("1"),
            produced_at=datetime(2026, 10, 4, 12, 0, 0),  # naive
        )


def test_zero_and_negative_quantity_rejected(session: Session) -> None:
    w = _world(session)
    for bad in ("0", "-3"):
        with pytest.raises(ValueError, match="positive"):
            record_production_output(
                session,
                ip_id=w["ip_a"],
                product_id=w["product"],
                quantity=Decimal(bad),
            )


def test_unknown_product_rejected(session: Session) -> None:
    w = _world(session)
    with pytest.raises(ValueError, match="not found"):
        record_production_output(session, ip_id=w["ip_a"], product_id=2**40, quantity=Decimal("1"))


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_api_post_output_and_finished_goods(engine: Engine) -> None:
    with engine.begin() as conn:
        w = _world(conn)
    aware = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
    with TestClient(app) as client:
        headers = _login(client, w["manager"])
        # make-to-stock
        r = client.post(
            "/production/output",
            json={
                "product_id": w["product"],
                "quantity": "4.5",
                "ip_id": w["ip_a"],
                "produced_at": aware.isoformat(),
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text
        assert r.json()["ip_id"] == w["ip_a"]
        # order-bound (ip derived from order)
        r = client.post(
            "/production/output",
            json={"product_id": w["product"], "quantity": 2, "order_id": w["order_a"]},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        # naive produced_at → 422
        r = client.post(
            "/production/output",
            json={
                "product_id": w["product"],
                "quantity": 1,
                "ip_id": w["ip_a"],
                "produced_at": "2026-10-04T12:00:00",
            },
            headers=headers,
        )
        assert r.status_code == 422, r.text
        # make-to-stock without ip_id → 422
        r = client.post(
            "/production/output",
            json={"product_id": w["product"], "quantity": 1},
            headers=headers,
        )
        assert r.status_code == 422, r.text
        # order from another IP → 404 (order not visible is fine; DI-001 would
        # also block) — here the manager CAN see order_b's id but its IP
        # differs; API derives order_b's ip and ensure_ip_access denies.
        r = client.post(
            "/production/output",
            json={"product_id": w["product"], "quantity": 1, "order_id": w["order_b"]},
            headers=headers,
        )
        assert r.status_code == 403, r.text
        # read back
        r = client.get("/production/finished-goods", params={"ip_id": w["ip_a"]}, headers=headers)
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["product_id"] == w["product"]
        assert Decimal(items[0]["produced_qty"]) == Decimal("6.5")
        assert Decimal(items[0]["on_hand"]) == Decimal("6.5")
        # cross-IP read → 403
        r = client.get("/production/finished-goods", params={"ip_id": w["ip_b"]}, headers=headers)
        assert r.status_code == 403, r.text
