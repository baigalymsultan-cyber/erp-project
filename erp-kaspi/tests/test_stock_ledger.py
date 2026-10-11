"""Tests for the inventory quantity ledger (P2-INV-001..004).

Real PostgreSQL (no mocks): the database is the enforcement layer and the
tests prove it — immutability (DI-005 STRONG), cross-IP blocks (DI-001),
the negative-stock gate, and the per-material serialization lock are all
exercised against live triggers, including a real two-connection
concurrency test (case 11).
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api import app
from app.core.security import hash_password
from app.services import add_order_material
from app.services.inventory import (
    record_movement,
    release_reservation,
    reserve_stock,
    set_order_status,
)

PASSWORD = "test-only-password"


def _world(session: Session | Connection, suffix: str | None = None) -> dict[str, int]:
    """Minimal stock world: two IPs, one material, one APPROVED order each."""
    sfx = suffix or uuid.uuid4().hex[:8]
    ip_a = session.execute(
        text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
        {"n": f"STOCK ip-a {sfx}", "b": f"{770000000000 + int(sfx, 16) % 10**9:012d}"},
    ).scalar_one()
    ip_b = session.execute(
        text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
        {"n": f"STOCK ip-b {sfx}", "b": f"{771000000000 + int(sfx, 16) % 10**9:012d}"},
    ).scalar_one()
    material = session.execute(
        text(
            "INSERT INTO materials (sku, name, unit, current_price) "
            "VALUES (:sku, 'Test board', 'pcs', 1000) RETURNING id"
        ),
        {"sku": f"STOCK-{sfx}"},
    ).scalar_one()
    order_a = session.execute(
        text(
            "INSERT INTO orders (ip_id, status, total_amount, source) "
            "VALUES (:ip, 'APPROVED', 0, 'MANUAL') RETURNING id"
        ),
        {"ip": ip_a},
    ).scalar_one()
    order_b = session.execute(
        text(
            "INSERT INTO orders (ip_id, status, total_amount, source) "
            "VALUES (:ip, 'APPROVED', 0, 'MANUAL') RETURNING id"
        ),
        {"ip": ip_b},
    ).scalar_one()
    # SHOP_MANAGER restricted to ip_a (via linked employee's assigned_ip_id)
    emp = session.execute(
        text(
            "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary) "
            "VALUES (:n, :ip, 'SALARY', 0) RETURNING id"
        ),
        {"n": f"STOCK mgr {sfx}", "ip": ip_a},
    ).scalar_one()
    username = f"stock-mgr-{sfx}"
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
    # No commit here: with a Session fixture the teardown rolls back;
    # with engine.begin() the block commits. Setup that must survive a
    # mid-test rollback uses engine.begin() explicitly (cases 2,4,7,8,10).
    return {
        "ip_a": ip_a,
        "ip_b": ip_b,
        "material": material,
        "order_a": order_a,
        "order_b": order_b,
        "manager": username,
    }


def _login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _on_hand(session: Session, ip_id: int, material_id: int) -> Decimal:
    return Decimal(
        session.execute(
            text("SELECT on_hand FROM v_material_stock WHERE ip_id = :ip AND material_id = :m"),
            {"ip": ip_id, "m": material_id},
        ).scalar_one_or_none()
        or 0
    )


def _durable_receipt(engine: Engine, qty: str = "100") -> dict[str, int]:
    """World + committed RECEIPT via engine.begin() (survives rollbacks)."""
    with engine.begin() as conn:
        w = _world(conn)
        conn.execute(
            text(
                "INSERT INTO stock_movements "
                "(ip_id, material_id, movement_date, quantity_delta, movement_type) "
                "VALUES (:ip, :m, CURRENT_DATE, :q, 'RECEIPT')"
            ),
            {"ip": w["ip_a"], "m": w["material"], "q": qty},
        )
    return w


# --------------------------------------------------------------------------- #
# 1. Ledger integrity: receipt → consumption, SUM equals the view
# --------------------------------------------------------------------------- #
def test_case01_receipt_then_consumption(session: Session) -> None:
    w = _world(session)
    record_movement(
        session,
        ip_id=w["ip_a"],
        material_id=w["material"],
        quantity_delta=Decimal("100"),
        movement_type="RECEIPT",
    )
    assert _on_hand(session, w["ip_a"], w["material"]) == Decimal("100")
    record_movement(
        session,
        ip_id=w["ip_a"],
        material_id=w["material"],
        quantity_delta=Decimal("-30"),
        movement_type="PRODUCTION_CONSUMPTION",
        order_id=w["order_a"],
    )
    assert _on_hand(session, w["ip_a"], w["material"]) == Decimal("70")
    total = session.execute(
        text(
            "SELECT SUM(quantity_delta) FROM stock_movements WHERE ip_id = :ip AND material_id = :m"
        ),
        {"ip": w["ip_a"], "m": w["material"]},
    ).scalar_one()
    assert Decimal(total) == Decimal("70")


# --------------------------------------------------------------------------- #
# 2. Immutability (DI-005 STRONG): UPDATE/DELETE blocked, GUC bypass fails
# --------------------------------------------------------------------------- #
def test_case02_ledger_immutable(engine: Engine, applied_schema: None) -> None:
    w = _durable_receipt(engine, "10")
    with engine.connect() as conn:
        mid = conn.execute(
            text("SELECT id FROM stock_movements WHERE ip_id = :ip"),
            {"ip": w["ip_a"]},
        ).scalar_one()

        # UPDATE blocked.
        with pytest.raises(DBAPIError, match="immutable"), conn.begin_nested():
            conn.execute(
                text("UPDATE stock_movements SET quantity_delta = 99 WHERE id = :i"),
                {"i": mid},
            )
        # DELETE blocked.
        with pytest.raises(DBAPIError, match="immutable"), conn.begin_nested():
            conn.execute(text("DELETE FROM stock_movements WHERE id = :i"), {"i": mid})
        # STRONG pattern: the GUC alone does NOT bypass for erp_app (not a
        # member of erp_migrator) — this is the DI-005 bypass fix (0018).
        conn.execute(text("SET app.allow_financial_delete = 'on'"))
        with pytest.raises(DBAPIError, match="immutable"), conn.begin_nested():
            conn.execute(text("DELETE FROM stock_movements WHERE id = :i"), {"i": mid})

    # The row is untouched.
    with engine.connect() as conn:
        on_hand = conn.execute(
            text("SELECT on_hand FROM v_material_stock WHERE ip_id = :ip AND material_id = :m"),
            {"ip": w["ip_a"], "m": w["material"]},
        ).scalar_one()
    assert Decimal(on_hand) == Decimal("10")


# --------------------------------------------------------------------------- #
# 3. DI-001: movement IP must equal the order's IP
# --------------------------------------------------------------------------- #
def test_case03_cross_ip_movement_blocked(session: Session) -> None:
    w = _world(session)
    with pytest.raises(DBAPIError, match="cross-IP"):
        record_movement(
            session,
            ip_id=w["ip_b"],  # movement claims IP-B ...
            material_id=w["material"],
            quantity_delta=Decimal("-5"),
            movement_type="PRODUCTION_CONSUMPTION",
            order_id=w["order_a"],  # ... but the order belongs to IP-A
        )
    session.rollback()


# --------------------------------------------------------------------------- #
# 4. Negative-stock gate: block over-consumption, allow exact depletion
# --------------------------------------------------------------------------- #
def test_case04_negative_stock_gate(engine: Engine, session: Session) -> None:
    w = _durable_receipt(engine, "10")
    # Over-consumption blocked ...
    with pytest.raises(DBAPIError, match="insufficient stock"):
        record_movement(
            session,
            ip_id=w["ip_a"],
            material_id=w["material"],
            quantity_delta=Decimal("-11"),
            movement_type="ISSUE",
            reason="over-issue attempt",
        )
    session.rollback()
    # ... exact depletion accepted; on-hand hits 0.
    record_movement(
        session,
        ip_id=w["ip_a"],
        material_id=w["material"],
        quantity_delta=Decimal("-10"),
        movement_type="ISSUE",
        reason="full issue",
    )
    session.flush()
    assert _on_hand(session, w["ip_a"], w["material"]) == Decimal("0")


# --------------------------------------------------------------------------- #
# 5. Sign / type / reason guards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        # zero delta
        {"movement_type": "RECEIPT", "quantity_delta": Decimal("0")},
        # wrong signs
        {"movement_type": "SCRAP", "quantity_delta": Decimal("5"), "reason": "x"},
        {"movement_type": "RECEIPT", "quantity_delta": Decimal("-5")},
        {"movement_type": "ISSUE", "quantity_delta": Decimal("5"), "reason": "x"},
        # missing reason
        {"movement_type": "ADJUSTMENT", "quantity_delta": Decimal("5")},
        {"movement_type": "SCRAP", "quantity_delta": Decimal("-5")},
        # PRODUCTION_* without order_id
        {"movement_type": "PRODUCTION_CONSUMPTION", "quantity_delta": Decimal("-5")},
        # RETURN without counterparty_id
        {"movement_type": "RETURN", "quantity_delta": Decimal("-5")},
        # unknown type
        {"movement_type": "RESERVATION", "quantity_delta": Decimal("5")},
    ],
)
def test_case05_sign_type_reason_guards(session: Session, kwargs: dict) -> None:
    w = _world(session)
    with pytest.raises(ValueError):
        record_movement(
            session,
            ip_id=w["ip_a"],
            material_id=w["material"],
            **kwargs,  # type: ignore[arg-type]
        )
    session.rollback()


# --------------------------------------------------------------------------- #
# 6. Idempotency: same (ref_type, ref_id) → one row
# --------------------------------------------------------------------------- #
def test_case06_idempotent_record_movement(session: Session) -> None:
    w = _world(session)
    first = record_movement(
        session,
        ip_id=w["ip_a"],
        material_id=w["material"],
        quantity_delta=Decimal("25"),
        movement_type="RECEIPT",
        ref_type="TEST_IMPORT",
        ref_id=424242,
    )
    second = record_movement(
        session,
        ip_id=w["ip_a"],
        material_id=w["material"],
        quantity_delta=Decimal("25"),
        movement_type="RECEIPT",
        ref_type="TEST_IMPORT",
        ref_id=424242,
    )
    assert first == second
    count = session.execute(
        text(
            "SELECT count(*) FROM stock_movements "
            "WHERE ref_type = 'TEST_IMPORT' AND ref_id = 424242"
        )
    ).scalar_one()
    assert count == 1


# --------------------------------------------------------------------------- #
# 7. add_order_material atomicity: gate failure rolls back both rows
# --------------------------------------------------------------------------- #
def test_case07_order_material_atomic_with_gate(engine: Engine, session: Session) -> None:
    from app.schemas import OrderMaterialCreate

    w = _durable_receipt(engine, "5")

    # 5 on hand; consuming 6 must fail the gate and leave NOTHING behind.
    with pytest.raises(DBAPIError, match="insufficient stock"):
        add_order_material(
            session,
            w["order_a"],
            OrderMaterialCreate(material_id=w["material"], quantity=Decimal("6")),
        )
    session.rollback()

    om_count = session.execute(
        text("SELECT count(*) FROM order_materials WHERE order_id = :o"),
        {"o": w["order_a"]},
    ).scalar_one()
    assert om_count == 0
    assert _on_hand(session, w["ip_a"], w["material"]) == Decimal("5")

    # A fitting consumption succeeds and writes BOTH rows in one transaction.
    om = add_order_material(
        session,
        w["order_a"],
        OrderMaterialCreate(material_id=w["material"], quantity=Decimal("5")),
    )
    session.flush()
    assert _on_hand(session, w["ip_a"], w["material"]) == Decimal("0")
    movement = session.execute(
        text(
            "SELECT movement_type, quantity_delta FROM stock_movements "
            "WHERE ref_type = 'ORDER_MATERIALS' AND ref_id = :r"
        ),
        {"r": om.id},
    ).one()
    assert movement.movement_type == "PRODUCTION_CONSUMPTION"
    assert Decimal(movement.quantity_delta) == Decimal("-5")


# --------------------------------------------------------------------------- #
# 8. Backfill: pre-ledger order_materials → CONSUMPTION rows, idempotent
# --------------------------------------------------------------------------- #
def test_case08_backfill_insert_is_idempotent(engine: Engine, session: Session) -> None:
    with engine.begin() as conn:
        w = _world(conn)
        # Seed order_materials WITHOUT the service (pre-ledger legacy rows).
        om_ids: list[int] = []
        for qty in ("3", "7"):
            om_id = conn.execute(
                text(
                    "INSERT INTO order_materials (order_id, material_id, quantity, price_at_time) "
                    "VALUES (:o, :m, :q, 1000) RETURNING id"
                ),
                {"o": w["order_a"], "m": w["material"], "q": qty},
            ).scalar_one()
            om_ids.append(om_id)
        conn.execute(
            text("UPDATE order_materials SET used_at = :d WHERE id = ANY(:ids)"),
            {"d": datetime(2026, 2, 10, 12, 0), "ids": om_ids},
        )
        # Cover the backfilled consumption with a receipt so the
        # negative-stock gate (active in this test) accepts the rows.
        # The real migration 0022 drops the trigger instead (verified on
        # the scratch DB); the INSERT..WHERE NOT EXISTS is identical.
        conn.execute(
            text(
                "INSERT INTO stock_movements "
                "(ip_id, material_id, movement_date, quantity_delta, movement_type) "
                "VALUES (:ip, :m, CURRENT_DATE, 10, 'RECEIPT')"
            ),
            {"ip": w["ip_a"], "m": w["material"]},
        )

    backfill_sql = text(
        """
        INSERT INTO stock_movements
            (ip_id, material_id, order_id, movement_date, quantity_delta,
             movement_type, ref_type, ref_id, reason, created_at)
        SELECT o.ip_id,
               om.material_id,
               om.order_id,
               om.used_at::date,
               -om.quantity,
               'PRODUCTION_CONSUMPTION',
               'ORDER_MATERIALS',
               om.id,
               'backfill: pre-ledger consumption (migration 0022)',
               om.created_at
        FROM order_materials om
        JOIN orders o ON o.id = om.order_id
        WHERE NOT EXISTS (
            SELECT 1 FROM stock_movements sm
            WHERE sm.ref_type = 'ORDER_MATERIALS' AND sm.ref_id = om.id
        )
        """
    )
    with engine.begin() as conn:
        conn.execute(backfill_sql)

    rows = session.execute(
        text(
            "SELECT ref_id, movement_date, quantity_delta FROM stock_movements "
            "WHERE ref_type = 'ORDER_MATERIALS' AND ref_id = ANY(:ids) "
            "ORDER BY ref_id"
        ),
        {"ids": om_ids},
    ).all()
    assert len(rows) == 2
    assert all(r.movement_date == date(2026, 2, 10) for r in rows)
    assert [Decimal(r.quantity_delta) for r in rows] == [Decimal("-3"), Decimal("-7")]

    # Re-run: idempotent, still exactly 2.
    with engine.begin() as conn:
        conn.execute(backfill_sql)
    count = session.execute(
        text(
            "SELECT count(*) FROM stock_movements "
            "WHERE ref_type = 'ORDER_MATERIALS' AND ref_id = ANY(:ids)"
        ),
        {"ids": om_ids},
    ).scalar_one()
    assert count == 2


# --------------------------------------------------------------------------- #
# 9. API: IP isolation on stock writes
# --------------------------------------------------------------------------- #
def test_case09_api_ip_isolation(engine: Engine, applied_schema: None) -> None:
    with engine.begin() as conn:
        sfx = uuid.uuid4().hex[:8]
        ip_a = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": f"API ip-a {sfx}", "b": f"{772000000000 + int(sfx, 16) % 10**9:012d}"},
        ).scalar_one()
        ip_b = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": f"API ip-b {sfx}", "b": f"{773000000000 + int(sfx, 16) % 10**9:012d}"},
        ).scalar_one()
        material = conn.execute(
            text(
                "INSERT INTO materials (sku, name, unit, current_price) "
                "VALUES (:sku, 'API board', 'pcs', 500) RETURNING id"
            ),
            {"sku": f"API-{sfx}"},
        ).scalar_one()
        order_b = conn.execute(
            text(
                "INSERT INTO orders (ip_id, status, total_amount, source) "
                "VALUES (:ip, 'APPROVED', 0, 'MANUAL') RETURNING id"
            ),
            {"ip": ip_b},
        ).scalar_one()
        emp = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary) "
                "VALUES (:n, :ip, 'SALARY', 0) RETURNING id"
            ),
            {"n": f"API mgr {sfx}", "ip": ip_a},
        ).scalar_one()
        username = f"api-mgr-{sfx}"
        user_id = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, employee_id, is_active) "
                "VALUES (:u, :h, :e, TRUE) RETURNING id"
            ),
            {"u": username, "h": hash_password(PASSWORD), "e": emp},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT :u, id FROM roles WHERE code = 'SHOP_MANAGER'"
            ),
            {"u": user_id},
        )

    with TestClient(app) as client:
        headers = _login(client, username)

        # Restricted manager of IP-A cannot receipt into IP-B.
        r = client.post(
            "/stock/receipts",
            json={"ip_id": ip_b, "material_id": material, "quantity": "10"},
            headers=headers,
        )
        assert r.status_code == 403, r.text

        # ... but can receipt into their own IP.
        r = client.post(
            "/stock/receipts",
            json={"ip_id": ip_a, "material_id": material, "quantity": "10"},
            headers=headers,
        )
        assert r.status_code == 201, r.text

        # Cross-IP reservation: order belongs to IP-B → 403.
        r = client.post(
            "/stock/reservations",
            json={"order_id": order_b, "material_id": material, "quantity": "5"},
            headers=headers,
        )
        assert r.status_code == 403, r.text

        # Reads are IP-scoped too.
        r = client.get("/stock/levels", params={"ip_id": ip_b}, headers=headers)
        assert r.status_code == 403, r.text
        r = client.get("/stock/levels", params={"ip_id": ip_a}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["items"][0]["on_hand"] == "10.000"


# --------------------------------------------------------------------------- #
# 10. Reservations: availability, release, expiry, order CANCELLED hook
# --------------------------------------------------------------------------- #
def test_case10_reservation_lifecycle(engine: Engine, session: Session) -> None:
    w = _durable_receipt(engine, "100")

    res = reserve_stock(
        session,
        order_id=w["order_a"],
        material_id=w["material"],
        quantity=Decimal("30"),
    )
    assert "warning" not in res
    session.flush()
    avail = session.execute(
        text(
            "SELECT on_hand, reserved_qty, available_qty "
            "FROM v_material_availability WHERE ip_id = :ip AND material_id = :m"
        ),
        {"ip": w["ip_a"], "m": w["material"]},
    ).one()
    assert Decimal(avail.on_hand) == Decimal("100")  # on-hand untouched
    assert Decimal(avail.reserved_qty) == Decimal("30")
    assert Decimal(avail.available_qty) == Decimal("70")

    # Over-reserve warns but still records.
    res2 = reserve_stock(
        session,
        order_id=w["order_a"],
        material_id=w["material"],
        quantity=Decimal("1000"),
    )
    assert res2["warning"] == "over_reserved"
    session.flush()

    # Release the first reservation → availability restored (minus over-reserve).
    release_reservation(session, int(res["reservation_id"]), "MANUAL")
    session.flush()
    avail = session.execute(
        text(
            "SELECT available_qty FROM v_material_availability "
            "WHERE ip_id = :ip AND material_id = :m"
        ),
        {"ip": w["ip_a"], "m": w["material"]},
    ).scalar_one()
    assert Decimal(avail) == Decimal("100") - Decimal("1000")

    # Expiry is read-time: an expired ACTIVE reservation is excluded.
    reserve_stock(
        session,
        order_id=w["order_a"],
        material_id=w["material"],
        quantity=Decimal("10"),
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.flush()
    avail = session.execute(
        text(
            "SELECT reserved_qty FROM v_material_availability "
            "WHERE ip_id = :ip AND material_id = :m"
        ),
        {"ip": w["ip_a"], "m": w["material"]},
    ).scalar_one()
    assert Decimal(avail) == Decimal("1000")  # expired 10 excluded

    # Order CANCELLED releases every ACTIVE reservation (service hook).
    set_order_status(session, w["order_a"], "CANCELLED")
    session.flush()
    active = session.execute(
        text(
            "SELECT count(*) FROM material_reservations WHERE order_id = :o AND status = 'ACTIVE'"
        ),
        {"o": w["order_a"]},
    ).scalar_one()
    assert active == 0
    released = session.execute(
        text(
            "SELECT count(*) FROM material_reservations "
            "WHERE order_id = :o AND status = 'RELEASED' "
            "AND released_reason = 'ORDER_CANCELLED'"
        ),
        {"o": w["order_a"]},
    ).scalar_one()
    assert released == 2  # the 1000 over-reserve + the expired 10


# --------------------------------------------------------------------------- #
# 11. Concurrency: two threads consume the last units — exactly one wins
# --------------------------------------------------------------------------- #
def test_case11_concurrent_consumption_race(engine: Engine, applied_schema: None) -> None:
    with engine.begin() as conn:
        sfx = uuid.uuid4().hex[:8]
        ip = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": f"RACE ip {sfx}", "b": f"{774000000000 + int(sfx, 16) % 10**9:012d}"},
        ).scalar_one()
        material = conn.execute(
            text(
                "INSERT INTO materials (sku, name, unit, current_price) "
                "VALUES (:sku, 'Race board', 'pcs', 100) RETURNING id"
            ),
            {"sku": f"RACE-{sfx}"},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO stock_movements "
                "(ip_id, material_id, movement_date, quantity_delta, movement_type) "
                "VALUES (:ip, :m, CURRENT_DATE, 10, 'RECEIPT')"
            ),
            {"ip": ip, "m": material},
        )

    results: dict[str, str] = {}

    def _consume(name: str) -> None:
        # Separate connection + transaction per thread: a real race.
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                sess = Session(bind=conn)
                record_movement(
                    sess,
                    ip_id=ip,
                    material_id=material,
                    quantity_delta=Decimal("-10"),
                    movement_type="ISSUE",
                    reason=f"race {name}",
                )
                trans.commit()
                results[name] = "ok"
            except Exception as exc:
                trans.rollback()
                results[name] = f"fail: {type(exc).__name__}"

    threads = [threading.Thread(target=_consume, args=(n,)) for n in ("t1", "t2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    # Exactly one thread committed; the other failed on the gate
    # (check_violation → IntegrityError, a DBAPIError subclass).
    assert sorted(results.values()) == ["fail: IntegrityError", "ok"], results
    with engine.connect() as conn:
        on_hand = conn.execute(
            text("SELECT on_hand FROM v_material_stock WHERE ip_id = :ip AND material_id = :m"),
            {"ip": ip, "m": material},
        ).scalar_one()
    assert Decimal(on_hand) == Decimal("0")  # never -10


# --------------------------------------------------------------------------- #
# 12. Compensating correction: SCRAP overstated → ADJUSTMENT fixes it
# --------------------------------------------------------------------------- #
def test_case12_compensating_correction_flow(session: Session) -> None:
    w = _world(session)
    record_movement(
        session,
        ip_id=w["ip_a"],
        material_id=w["material"],
        quantity_delta=Decimal("100"),
        movement_type="RECEIPT",
    )
    scrap_id = record_movement(
        session,
        ip_id=w["ip_a"],
        material_id=w["material"],
        quantity_delta=Decimal("-20"),
        movement_type="SCRAP",
        reason="damaged batch",
    )
    assert _on_hand(session, w["ip_a"], w["material"]) == Decimal("80")
    # Correction is a NEW row (§5), never an UPDATE (case 2 proves UPDATE dies).
    record_movement(
        session,
        ip_id=w["ip_a"],
        material_id=w["material"],
        quantity_delta=Decimal("8"),
        movement_type="ADJUSTMENT",
        reason=f"scrap correction, movement #{scrap_id}: only 12 were damaged",
    )
    session.commit()
    assert _on_hand(session, w["ip_a"], w["material"]) == Decimal("88")
    count = session.execute(
        text("SELECT count(*) FROM stock_movements WHERE ip_id = :ip AND material_id = :m"),
        {"ip": w["ip_a"], "m": w["material"]},
    ).scalar_one()
    assert count == 3  # receipt + scrap + adjustment: full audit trail
