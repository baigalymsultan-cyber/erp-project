"""
Regression tests for the production-readiness audit fixes.

Covers:
  * multi-IP isolation (OWASP API1) — a restricted principal can act only on
    the ИП assigned to their employee, and gets 403 everywhere else;
  * POST /orders persists OrderItems and rejects inconsistent totals;
  * the API OrderStatus enum matches chk_orders_status;
  * the login rate limiter is keyed per client+username, not globally;
  * account lockout via users.locked_until;
  * PayrollPeriodCreate rejects inverted dates at the API boundary;
  * POST /kaspi/payouts uses the payout contract, not OrderCreate.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import app
from app.schemas import OrderStatus

RUN = uuid.uuid4().hex[:12]
RUN_DIGITS = str(int(RUN, 16) % 100_000_000_000).zfill(11)
#: unique 12-digit БИНs within one run (chk_ip_entities_bin_digits)
_IP_SEQ = iter("123456789")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _seed_ip(engine, suffix: str = "") -> int:
    bin_digits = f"{next(_IP_SEQ)}{RUN_DIGITS}"
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"
            ),
            {"n": f"AUDIT ИП {RUN} {suffix}", "b": bin_digits},
        ).scalar_one()


def _seed_employee(engine, ip_id: int) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary) "
                "VALUES (:n, :ip, 'SALARY', 0) RETURNING id"
            ),
            {"n": f"AUDIT employee {RUN}", "ip": ip_id},
        ).scalar_one()


def _seed_user_with_role(
    engine,
    username: str,
    password: str,
    role: str,
    employee_id: int | None = None,
) -> None:
    from app.core.security import hash_password

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash, employee_id) "
                "VALUES (:u, :h, :e) ON CONFLICT (username) DO UPDATE "
                "SET employee_id = EXCLUDED.employee_id"
            ),
            {"u": username, "h": hash_password(password), "e": employee_id},
        )
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT u.id, r.id FROM users u, roles r "
                "WHERE u.username = :u AND r.code = :r "
                "ON CONFLICT DO NOTHING"
            ),
            {"u": username, "r": role},
        )


def _login_headers(username: str, password: str) -> dict[str, str]:
    with TestClient(app) as client:
        r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# --------------------------------------------------------------------------- #
# 1. Order state machine + items
# --------------------------------------------------------------------------- #


def test_api_order_status_matches_database():
    """The API enum must be the DB state machine — one vocabulary, not two."""
    from app.models.orders import ORDER_STATUSES

    assert {s.value for s in OrderStatus} == set(ORDER_STATUSES)


def test_order_items_are_persisted_and_inconsistent_total_rejected(
    engine, applied_schema, admin_headers
):
    ip = _seed_ip(engine, "1")
    items = [
        {"product_name": "Диван", "quantity": "2.000", "unit_price": "15000.00"},
        {"product_name": "Кресло", "quantity": "1.000", "unit_price": "5000.00"},
    ]
    with TestClient(app) as client:
        ok = client.post(
            "/orders",
            json={
                "ip_id": ip,
                "external_order_id": f"ORD-{RUN}",
                "status": "NEW",
                "total_amount": "35000.00",
                "source": "KASPI_PAY",
                "items": items,
            },
            headers=admin_headers,
        )
        assert ok.status_code == 201, ok.text
        order_id = ok.json()["id"]
        assert ok.json()["consistent"] is True

        with engine.connect() as conn:
            saved = conn.execute(
                text("SELECT count(*) FROM order_items WHERE order_id = :i"),
                {"i": order_id},
            ).scalar_one()
        assert saved == 2, "OrderItems must be persisted, not just counted"

        bad = client.post(
            "/orders",
            json={
                "ip_id": ip,
                "external_order_id": f"ORD-BAD-{RUN}",
                "status": "NEW",
                "total_amount": "99999.00",
                "source": "KASPI_PAY",
                "items": items,
            },
            headers=admin_headers,
        )
        assert bad.status_code == 422, bad.text
        assert "items_total" in bad.json()["detail"]


# --------------------------------------------------------------------------- #
# 2. Multi-IP isolation (OWASP API1)
# --------------------------------------------------------------------------- #


def test_multi_ip_isolation(engine, applied_schema, admin_headers):
    """user A (employee linked to ИП A) reads ИП A but is denied ИП B."""
    ip_a = _seed_ip(engine, "A")
    ip_b = _seed_ip(engine, "B")
    emp = _seed_employee(engine, ip_a)
    username = f"iso-viewer-{RUN}"
    password = "Iso-View-2026!"
    _seed_user_with_role(engine, username, password, "VIEWER", employee_id=emp)
    headers = _login_headers(username, password)

    # an operation that belongs to ИП B
    with engine.connect() as conn:
        type_id = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_PAY'")
        ).scalar_one()
    with engine.begin() as conn:
        op_b = conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, comment) "
                "VALUES (:ip, :t, 100.00, '2026-09-01', 'iso') RETURNING id"
            ),
            {"ip": ip_b, "t": type_id},
        ).scalar_one()

    with TestClient(app) as client:
        # P1-2 (2026-10-03): read of an object in a foreign ИП → 404, not 403.
        # Returning 403 would create an existence oracle (404 = missing,
        # 403 = exists-but-foreign). For IP-restricted principals, out-of-scope
        # objects are indistinguishable from missing.
        r_b = client.get(f"/operations/{op_b}", headers=headers)
        assert r_b.status_code == 404, r_b.text
        # writing into a foreign ИП → 403
        w_b = client.post(
            "/orders",
            json={
                "ip_id": ip_b,
                "external_order_id": f"ISO-{RUN}",
                "status": "NEW",
                "total_amount": "100.00",
                "source": "KASPI_PAY",
                "items": [{"product_name": "x", "quantity": "1.000", "unit_price": "100.00"}],
            },
            headers=headers,
        )
        assert w_b.status_code == 403, w_b.text
        # the same user passes when the object belongs to their own ИП
        with engine.begin() as conn:
            op_a = conn.execute(
                text(
                    "INSERT INTO operations (ip_id, type_id, amount, operation_date, comment) "
                    "VALUES (:ip, :t, 50.00, '2026-09-01', 'iso') RETURNING id"
                ),
                {"ip": ip_a, "t": type_id},
            ).scalar_one()
        r_a = client.get(f"/operations/{op_a}", headers=headers)
        assert r_a.status_code == 200, r_a.text

        # ADMIN is unrestricted: reads the same foreign-ИП object
        r_admin = client.get(f"/operations/{op_b}", headers=admin_headers)
        assert r_admin.status_code == 200, r_admin.text


# --------------------------------------------------------------------------- #
# 3. Login rate limiting and account lockout
# --------------------------------------------------------------------------- #


def test_login_rate_limit_is_per_client_and_username(engine, applied_schema):
    """Five bad attempts lock the (ip, username) bucket — a different username
    from the same client must still be able to log in (audit #8)."""
    from app.core.security import reset_auth_rate_limit

    reset_auth_rate_limit()
    victim = f"lim-{RUN}"
    _seed_user_with_role(engine, victim, "Right-Pass-2026!", "VIEWER")
    other = f"lim2-{RUN}"
    _seed_user_with_role(engine, other, "Other-Pass-2026!", "VIEWER")

    with TestClient(app) as client:
        for _ in range(5):
            client.post(
                "/auth/login",
                json={"username": victim, "password": "wrong-pass"},
            )
        r6 = client.post(
            "/auth/login", json={"username": victim, "password": "Right-Pass-2026!"}
        )
        assert r6.status_code == 429, r6.text
        # another username, same "client" — not affected by the first bucket
        r_other = client.post(
            "/auth/login", json={"username": other, "password": "Other-Pass-2026!"}
        )
        assert r_other.status_code == 200, r_other.text
    reset_auth_rate_limit()


def test_account_lockout_after_repeated_failures(engine, applied_schema):
    """§55/audit #9 — locked_until is enforced: after the threshold even the
    correct password is refused until the lock expires."""
    from app.core.security import reset_auth_rate_limit

    reset_auth_rate_limit()
    username = f"lock-{RUN}"
    password = "Unlock-Me-2026!"
    _seed_user_with_role(engine, username, password, "VIEWER")

    with TestClient(app) as client:
        for _ in range(5):
            client.post(
                "/auth/login", json={"username": username, "password": "nope"}
            )
        # the (ip, username) limiter bucket is full now; clear only it so the
        # next request exercises the account lockout, not the rate limiter
        from app.core.security import _auth_attempts

        _auth_attempts.clear()
        locked = client.post(
            "/auth/login", json={"username": username, "password": password}
        )
        assert locked.status_code == 423, locked.text

        # the account row carries the lock
        with engine.connect() as conn:
            locked_until = conn.execute(
                text("SELECT locked_until FROM users WHERE username = :u"),
                {"u": username},
            ).scalar_one()
        assert locked_until is not None
    reset_auth_rate_limit()


# --------------------------------------------------------------------------- #
# 4. Validation at the API boundary
# --------------------------------------------------------------------------- #


def test_payroll_period_rejects_inverted_dates(applied_schema, admin_headers):
    with TestClient(app) as client:
        r = client.post(
            "/payroll/periods",
            json={"period_start": "2026-09-10", "period_end": "2026-09-01"},
            headers=admin_headers,
        )
    assert r.status_code == 422, r.text


def test_kaspi_payout_uses_payout_schema(applied_schema, admin_headers, engine):
    ip = _seed_ip(engine, "P")
    with TestClient(app) as client:
        ok = client.post(
            "/kaspi/payouts",
            json={
                "ip_id": ip,
                "external_payout_id": f"PO-{RUN}",
                "payout_date": "2026-09-01",
                "gross_amount": "5000.00",
                "commission_amount": "250.00",
            },
            headers=admin_headers,
        )
        assert ok.status_code == 201, ok.text
        # order-shaped payloads are no longer accepted here
        bad = client.post(
            "/kaspi/payouts",
            json={
                "ip_id": ip,
                "external_order_id": f"ORD-{RUN}",
                "total_amount": "5000.00",
                "source": "KASPI_SHOP",
                "items": [{"product_name": "x", "quantity": "1.000", "unit_price": "1.00"}],
            },
            headers=admin_headers,
        )
        assert bad.status_code == 422, bad.text


def test_payroll_periods_audit_trigger_exists(engine, applied_schema):
    """A-1: payroll_periods status transitions must be audited.
    The period status is a financial control gate; changes were invisible
    in audit_log before migration 0009."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_payroll_periods_audit' "
                "AND tgrelid = 'payroll_periods'::regclass"
            )
        ).scalar_one_or_none()
    assert result == 1, "trg_payroll_periods_audit missing on payroll_periods"


def test_audit_triggers_cover_provenance_and_reference_tables(engine, applied_schema):
    """A-2/A-3/A-4: provenance (import_batches, raw_kaspi_payloads), order
    line items (order_items, order_materials, order_status_history), and
    reference data (categories, counterparties, operation_types,
    work_operation_types) must have audit triggers (migration 0010)."""
    tables = [
        "import_batches", "raw_kaspi_payloads",
        "order_items", "order_materials", "order_status_history",
        "categories", "counterparties", "operation_types", "work_operation_types",
    ]
    with engine.connect() as conn:
        for table in tables:
            result = conn.execute(
                text(
                    "SELECT 1 FROM pg_trigger "
                    f"WHERE tgname = 'trg_{table}_audit' "
                    f"AND tgrelid = '{table}'::regclass"
                )
            ).scalar_one_or_none()
            assert result == 1, f"trg_{table}_audit missing on {table}"
