"""F-ETL-010: POST /kaspi/payouts must be idempotent on duplicate.

uq_kaspi_payouts_external (ip_id, external_payout_id) exists, but a duplicate
POST used to surface as a raw 500. Retry-after-timeout (the normal case for
operators/webhooks) must be a safe no-op: 200 with the existing row.
"""

import random

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import app
from app.core.security import hash_password


def _make_ip(engine, name):
    bin_code = f"313{random.randint(100000000, 999999999)}"
    with engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": name, "b": bin_code},
        ).scalar_one()


def _make_accountant(engine, ip_id, username):
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        emp_id = session.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary) "
                "VALUES (:n, :ip, 'SALARY', 0) RETURNING id"
            ),
            {"n": username, "ip": ip_id},
        ).scalar_one()
        session.execute(
            text("INSERT INTO users (username, password_hash, employee_id) VALUES (:u, :h, :e)"),
            {"u": username, "h": hash_password("testpw123"), "e": emp_id},
        )
        session.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT u.id, r.id FROM users u, roles r "
                "WHERE u.username = :u AND r.code = 'ACCOUNTANT' ON CONFLICT DO NOTHING"
            ),
            {"u": username},
        )
        session.commit()


def _login(client, username):
    r = client.post("/auth/login", json={"username": username, "password": "testpw123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _payload(ip_id, ext):
    return {
        "ip_id": ip_id,
        "external_payout_id": ext,
        "payout_date": "2026-02-01",
        "gross_amount": "10000.00",
        "commission_amount": "500.00",
        "status": "EXPECTED",
    }


def test_duplicate_payout_post_returns_existing_not_500(engine, applied_schema):
    """F-ETL-010: second POST with the same (ip_id, external_payout_id)
    returns 200 with the existing row — never 500; only one row exists."""
    ip_id = _make_ip(engine, "PAYOUT-IDEMP")
    username = f"payoutacct{random.randint(0, 999999)}"
    _make_accountant(engine, ip_id, username)

    with TestClient(app) as client:
        headers = _login(client, username)
        first = client.post("/kaspi/payouts", json=_payload(ip_id, "EXT-001"), headers=headers)
        assert first.status_code == 201, first.text
        second = client.post("/kaspi/payouts", json=_payload(ip_id, "EXT-001"), headers=headers)
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first.json()["id"]
        assert second.json().get("idempotent_replay") is True

    with engine.begin() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM kaspi_payouts "
                "WHERE ip_id = :ip AND external_payout_id = 'EXT-001'"
            ),
            {"ip": ip_id},
        ).scalar_one()
    assert count == 1


def test_same_external_id_different_ip_isolation(engine, applied_schema):
    """Cross-IP isolation: the same external_payout_id on another IP is a
    distinct payout (201), not a conflict."""
    ip_a = _make_ip(engine, "PAYOUT-IPA")
    ip_b = _make_ip(engine, "PAYOUT-IPB")
    username = f"payoutiso{random.randint(0, 999999)}"
    _make_accountant(engine, ip_a, username + "a")
    _make_accountant(engine, ip_b, username + "b")

    with TestClient(app) as client:
        ha = _login(client, username + "a")
        hb = _login(client, username + "b")
        r1 = client.post("/kaspi/payouts", json=_payload(ip_a, "SHARED-EXT"), headers=ha)
        assert r1.status_code == 201, r1.text
        r2 = client.post("/kaspi/payouts", json=_payload(ip_b, "SHARED-EXT"), headers=hb)
        assert r2.status_code == 201, r2.text
        assert r2.json()["id"] != r1.json()["id"]

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM kaspi_payouts WHERE external_payout_id = 'SHARED-EXT'"),
        ).scalar_one()
    assert count == 2


def test_foreign_ip_payout_post_still_403(engine, applied_schema):
    """A restricted accountant from another IP still gets 403 (not 200/201)."""
    ip_a = _make_ip(engine, "PAYOUT-F-A")
    ip_b = _make_ip(engine, "PAYOUT-F-B")
    username = f"payoutf{random.randint(0, 999999)}"
    _make_accountant(engine, ip_b, username)

    with TestClient(app) as client:
        headers = _login(client, username)
        r = client.post("/kaspi/payouts", json=_payload(ip_a, "FOR-EXT"), headers=headers)
        assert r.status_code == 403, r.text
