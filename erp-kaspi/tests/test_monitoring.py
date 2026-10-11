"""Monitoring summary — §72 (REQ-0325).

Covers every controlled signal: application errors, database state, Kaspi
API errors / failed syncs / stale jobs, authentication failures. Rows that
the FastAPI suite must read are committed (the endpoint runs on its own
connection); the §94 session-end purge removes them afterwards.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api import APP_5XX, app
from app.core.security import hash_password

#: unique run suffix — repeated runs stay inside the unique constraints
RUN = uuid.uuid4().hex[:12]


def _seed_ip(engine: Engine) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) VALUES ('MON QA ИП', '688888888888') "
                "ON CONFLICT (bin) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            )
        ).scalar_one()


def _sync(engine: Engine, ip_id: int, status: str, errors: int, started: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sync_log (ip_id, api_type, status, sync_started_at, "
                "sync_finished_at, records_fetched, records_inserted, records_updated, "
                "errors_count, error_details) "
                "VALUES (:ip, 'SHOP', :status, :started, now(), 3, 3, 0, :errors, :details)"
            ),
            {
                "ip": ip_id,
                "status": status,
                "started": started,
                "errors": errors,
                "details": None if status == "SUCCESS" else '{"e": "boom"}',
            },
        )


def test_summary_requires_auth(applied_schema):
    with TestClient(app) as client:
        response = client.get("/monitoring/summary")
    assert response.status_code == 401


def test_summary_reports_sync_and_auth(engine, applied_schema, admin_headers):
    """Signals come from sync_log (24h window) and users.failed_logins."""
    ip_id = _seed_ip(engine)
    now = datetime.now(UTC)
    _sync(engine, ip_id, status="FAILED", errors=4, started=now - timedelta(hours=1))
    _sync(engine, ip_id, status="PARTIAL", errors=1, started=now - timedelta(hours=2))
    # Outside the 24h window — must not be counted.
    _sync(
        engine,
        ip_id,
        status="FAILED",
        errors=99,
        started=now - timedelta(days=3),
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash, failed_logins) "
                "VALUES (:u, :h, 3) ON CONFLICT (username) DO UPDATE "
                "SET failed_logins = 3"
            ),
            {"u": f"mon-fail-{RUN}", "h": hash_password("Mon-Fail-2026!x")},
        )

    with TestClient(app) as client:
        response = client.get("/monitoring/summary", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["database"]["ok"] is True
    assert body["database"]["missing_tables"] == []
    assert body["application"]["errors_5xx_since_start"] == APP_5XX.count

    sync = body["kaspi_sync_last_24h"]
    assert sync["failed"] >= 1
    assert sync["partial"] >= 1
    assert sync["errors_total"] >= 5
    # The 3-day-old FAILED sync and its 99 errors are excluded.
    assert sync["errors_total"] < 99

    assert body["auth"]["failed_logins_total"] >= 3
    assert body["auth"]["users_with_failures"] >= 1


def test_failed_login_increments_counter(engine, applied_schema, admin_headers):
    """§72 — an authentication failure is recorded on the account."""
    username = f"mon-login-{RUN}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash) VALUES (:u, :h) "
                "ON CONFLICT (username) DO NOTHING"
            ),
            {"u": username, "h": hash_password("Mon-Login-2026!x")},
        )

    with TestClient(app) as client:
        bad = client.post(
            "/auth/login",
            json={"username": username, "password": "wrong-password"},
        )
        assert bad.status_code == 401

    with engine.begin() as conn:
        fails = conn.execute(
            text("SELECT failed_logins FROM users WHERE username = :u"),
            {"u": username},
        ).scalar_one()
    assert fails == 1
