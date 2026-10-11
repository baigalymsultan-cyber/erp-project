"""
API smoke tests — FastAPI app against the real PostgreSQL schema (§59, §68).

The TestClient executes the actual ASGI app; every POST lands in the real
database (no mocks). Fixtures are unique per run so repeated runs stay
idempotent (TD-012 lesson).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import app

#: unique run suffix — keeps repeated runs inside the unique constraints
RUN = uuid.uuid4().hex[:12]
#: digits-only variant for 12-digit БИН columns (chk_ip_entities_bin_digits)
RUN_DIGITS = str(int(RUN, 16) % 100_000_000_000).zfill(11)


def test_health_reports_ok(engine, applied_schema):
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok", body
    assert body["missing_tables"] == []


def test_operation_roundtrip_is_append_only(engine, applied_schema, admin_headers):
    with TestClient(app) as client:
        r = client.post(
            "/operations",
            json={
                "ip_id": _seed_ip(engine),
                "type_id": _type_id(engine, "SALE_KASPI_PAY"),
                "direction": "IN",
                "amount": "12345.67",
                "operation_date": "2026-09-01",
                "comment": f"api {RUN}",
            },
            headers=admin_headers,
        )
        assert r.status_code == 201, r.text
        op_id = r.json()["id"]
        got = client.get(f"/operations/{op_id}", headers=admin_headers).json()
        assert got["amount"] == "12345.67"
        assert got["direction"] == "IN"  # schema vocabulary: operation_types.direction


def _seed_account(engine, ip_id: int, label: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO accounts (ip_id, type, name) "
                    "VALUES (:ip_id, 'OTHER', :name) RETURNING id"
                ),
                {"ip_id": ip_id, "name": f"{label}-{RUN}"},
            ).scalar_one()
        )


def test_pnl_excludes_transfers(engine, applied_schema, admin_headers):
    from_ip = _seed_ip(engine)
    to_ip = _seed_counterpart_ip(engine)
    from_account_id = _seed_account(engine, from_ip, "transfer-from")
    to_account_id = _seed_account(engine, to_ip, "transfer-to")
    with TestClient(app) as client:
        before = client.get("/pnl", headers=admin_headers).json()
        r = client.post(
            "/transfers",
            json={
                "from_ip_id": from_ip,
                "to_ip_id": to_ip,
                "from_account_id": from_account_id,
                "to_account_id": to_account_id,
                "amount": "777.00",
                "transfer_date": "2026-09-01",
            },
            headers={**admin_headers, "Idempotency-Key": f"pnl-transfer-{RUN}"},
        )
        assert r.status_code == 201, r.text
        after = client.get("/pnl", headers=admin_headers).json()
    assert before["net_profit"] == after["net_profit"], "§63 transfers out of P&L"


def test_self_transfer_rejected(engine, applied_schema, admin_headers):
    ip = _seed_ip(engine)
    from_account_id = _seed_account(engine, ip, "self-transfer-from")
    to_account_id = _seed_account(engine, ip, "self-transfer-to")
    with TestClient(app) as client:
        r = client.post(
            "/transfers",
            json={
                "from_ip_id": ip,
                "to_ip_id": ip,
                "from_account_id": from_account_id,
                "to_account_id": to_account_id,
                "amount": "1.00",
                "transfer_date": "2026-09-01",
            },
            headers={**admin_headers, "Idempotency-Key": f"self-transfer-{RUN}"},
        )
    assert r.status_code == 422, r.text


def test_rbac_is_enforced_in_backend(engine, applied_schema, admin_headers):
    """§53 — the check lives in the API, not the UI.

    Unauthenticated requests are 401 on every business endpoint; a token
    without a sufficient role is 403; ADMIN passes; /health stays public.
    """
    payload = {
        "ip_id": _seed_ip(engine),
        "type_id": _type_id(engine, "SALE_KASPI_PAY"),
        "direction": "IN",
        "amount": "10.00",
        "operation_date": "2026-09-01",
    }
    with TestClient(app) as client:
        # no token at all
        assert client.post("/operations", json=payload).status_code == 401
        assert client.get("/pnl").status_code == 401
        assert client.get("/kaspi/payouts").status_code == 401
        # /health stays public: monitoring must not need credentials (§70)
        assert client.get("/health").status_code == 200
        # a VIEWER token is authenticated but not authorised to write
        viewer_headers = _viewer_headers(engine)
        assert client.post("/operations", json=payload, headers=viewer_headers).status_code == 403
        # reads are allowed for VIEWER+
        assert client.get("/pnl", headers=viewer_headers).status_code == 200
        # ADMIN passes
        assert client.post("/operations", json=payload, headers=admin_headers).status_code == 201


def _viewer_headers(engine) -> dict[str, str]:
    """Log in as a VIEWER-only user; the login limiter allows 3 per run."""
    from app.core.security import hash_password

    username = f"viewer-api-{RUN}"
    password = "Viewer-Pass-2026!"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash) "
                "VALUES (:u, :h) ON CONFLICT (username) DO NOTHING"
            ),
            {"u": username, "h": hash_password(password)},
        )
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT u.id, r.id FROM users u, roles r "
                "WHERE u.username = :u AND r.code = 'VIEWER' ON CONFLICT DO NOTHING"
            ),
            {"u": username},
        )
    with TestClient(app) as client:
        r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _seed_ip(engine) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) "
                "VALUES (:n, :b) "
                "ON CONFLICT (bin) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id"
            ),
            {"n": f"API ИП {RUN}", "b": f"9{RUN_DIGITS}"},
        ).scalar_one()


def _seed_counterpart_ip(engine) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) "
                "VALUES (:n, :b) "
                "ON CONFLICT (bin) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id"
            ),
            {"n": f"API ИП-2 {RUN}", "b": f"8{RUN_DIGITS}"},
        ).scalar_one()


def _type_id(engine, code: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT id FROM operation_types WHERE code = :c"), {"c": code}
        ).scalar_one()


def test_operation_reversal(engine, applied_schema, admin_headers):
    """§16 — reversal keeps the row, flips status to REVERSED; P&L excludes it."""
    ip = _seed_ip(engine)
    with TestClient(app) as client:
        before = client.get(
            "/pnl",
            params={"period_start": "2026-09-01", "period_end": "2026-09-30"},
            headers=admin_headers,
        ).json()
        created = client.post(
            "/operations",
            json={
                "ip_id": ip,
                "type_id": _type_id(engine, "SALE_KASPI_PAY"),
                "direction": "IN",
                "amount": "1000.00",
                "operation_date": "2026-09-05",
                "comment": f"rev {RUN}",
            },
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text
        op_id = created.json()["id"]
        reversed_response = client.post(
            f"/operations/{op_id}/reverse",
            params={"comment": f"reversal {RUN}"},
            headers=admin_headers,
        )
        assert reversed_response.status_code == 200, reversed_response.text
        assert reversed_response.json()["status"] == "REVERSED"
        after = client.get(
            "/pnl",
            params={"period_start": "2026-09-01", "period_end": "2026-09-30"},
            headers=admin_headers,
        ).json()
    assert after["total_income"] == before["total_income"], (
        "§16: a REVERSED operation must be excluded from P&L totals"
    )
    stored_status = (
        engine.connect()
        .execute(text("SELECT status FROM operations WHERE id = :i"), {"i": op_id})
        .scalar_one()
    )
    assert stored_status == "REVERSED", "§16: history is kept, the row is never deleted"


def test_employees_status_filter_alias(engine, applied_schema, admin_headers):
    """M-1: /employees must accept ?status=ACTIVE (alias for status_filter),
    consistent with /orders. Without the alias, FastAPI silently drops the
    param and FIRED/ON_LEAVE employees leak into the foreman dropdown."""
    from app.api import app

    # The alias must be registered on the route (no HTTP call needed)
    route = next(r for r in app.routes if getattr(r, "path", "") == "/employees")
    param_names = set()
    for param in route.dependant.query_params:
        param_names.add(param.alias or param.name)
    assert "status" in param_names, f"/employees query params lack 'status' alias: {param_names}"


def test_post_operation_idempotency_key(engine, applied_schema, admin_headers):
    """C-1: POST /operations with Idempotency-Key header must be idempotent.
    A retried request with the same key returns the existing operation
    instead of creating a duplicate ledger entry."""
    from fastapi.testclient import TestClient

    from app.api import app

    with TestClient(app) as client:
        # Seed an IP and operation type
        with engine.begin() as conn:
            ip_id = conn.execute(
                text(
                    "INSERT INTO ip_entities (name, bin) "
                    "VALUES ('C1-TEST', '123456789012') RETURNING id"
                )
            ).scalar_one()
            type_id = conn.execute(
                text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_SHOP'")
            ).scalar_one()

        payload = {
            "ip_id": ip_id,
            "type_id": type_id,
            "direction": "IN",
            "amount": "1000.00",
            "operation_date": "2026-10-03",
            "comment": "C-1 idempotency test",
        }
        headers = {**admin_headers, "Idempotency-Key": f"c1-test-{uuid.uuid4().hex}"}

        r1 = client.post("/operations", json=payload, headers=headers)
        assert r1.status_code == 201, r1.text
        id1 = r1.json()["id"]

        # Retry with same key → same ID, no duplicate
        r2 = client.post("/operations", json=payload, headers=headers)
        assert r2.status_code == 201, r2.text
        assert r2.json()["id"] == id1
        assert r2.json()["status"] == "IDEMPOTENT_REPLAY"


def test_post_operation_wrong_sign_returns_422(engine, applied_schema, admin_headers):
    """P2-4: OUT operation with positive amount must return 422 (not 500).
    The §27 sign convention is validated in the service layer."""
    from fastapi.testclient import TestClient

    from app.api import app

    with TestClient(app) as client:
        with engine.begin() as conn:
            ip_id = conn.execute(
                text(
                    "INSERT INTO ip_entities (name, bin) "
                    "VALUES ('P24-TEST', '123456789013') RETURNING id"
                )
            ).scalar_one()
            type_id = conn.execute(
                text("SELECT id FROM operation_types WHERE direction = 'OUT' LIMIT 1")
            ).scalar_one()

        resp = client.post(
            "/operations",
            headers=admin_headers,
            json={
                "ip_id": ip_id,
                "type_id": type_id,
                "direction": "OUT",
                "amount": "1000.00",  # wrong sign for OUT
                "operation_date": "2026-10-03",
            },
        )
    assert resp.status_code == 422, resp.text
    assert "§27" in resp.json()["detail"]


def test_idempotency_replay_scoped_to_ip(engine, applied_schema, admin_headers):
    """P1-1/M-1: replay lookup is scoped to the request's IP.
    A fingerprint from another IP must NOT return that IP's operation."""
    from fastapi.testclient import TestClient

    from app.api import app

    with TestClient(app) as client:
        with engine.begin() as conn:
            ip_a = conn.execute(
                text(
                    "INSERT INTO ip_entities (name, bin) "
                    "VALUES ('P11-A', '123456789014') RETURNING id"
                )
            ).scalar_one()
            ip_b = conn.execute(
                text(
                    "INSERT INTO ip_entities (name, bin) "
                    "VALUES ('P11-B', '123456789015') RETURNING id"
                )
            ).scalar_one()
            type_id = conn.execute(
                text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_SHOP'")
            ).scalar_one()

        key = f"p11-test-{uuid.uuid4().hex}"
        # Create operation under IP-A with the key
        r1 = client.post(
            "/operations",
            headers={**admin_headers, "Idempotency-Key": key},
            json={
                "ip_id": ip_a,
                "type_id": type_id,
                "direction": "IN",
                "amount": "500.00",
                "operation_date": "2026-10-03",
            },
        )
        assert r1.status_code == 201, r1.text
        op_a_id = r1.json()["id"]

        # Same key but IP-B: must NOT replay IP-A's operation.
        # It creates a new operation under IP-B (fingerprint is per-IP now).
        r2 = client.post(
            "/operations",
            headers={**admin_headers, "Idempotency-Key": key},
            json={
                "ip_id": ip_b,
                "type_id": type_id,
                "direction": "IN",
                "amount": "500.00",
                "operation_date": "2026-10-03",
            },
        )
        assert r2.status_code == 201, r2.text
        assert r2.json()["id"] != op_a_id, "cross-IP replay leak!"
        assert r2.json()["status"] == "APPEND_ONLY"
