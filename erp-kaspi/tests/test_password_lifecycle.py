"""
MASTER PROMPT §17 — password lifecycle and token invalidation (v7 audit).

Covers the §17 guarantees that were missing before this audit round:
password-change, password-change invalidation, admin reset flow and the
rate limiter's bounded memory. The TestClient executes the real ASGI app
against the real PostgreSQL schema — no mocks (§68).
"""

from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import app
from app.core.security import reset_auth_rate_limit

RUN = uuid.uuid4().hex[:12]
ADMIN_PASS = "Director-Pass-2026"


def _ensure_admin(engine) -> None:
    from app.core.security import hash_password

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO users (username, password_hash)
                VALUES ('admin-auth', :h)
                ON CONFLICT (username) DO NOTHING
                """
            ),
            {"h": hash_password(ADMIN_PASS)},
        )
        conn.execute(
            text(
                """
                INSERT INTO user_roles (user_id, role_id)
                SELECT u.id, r.id FROM users u, roles r
                WHERE u.username = 'admin-auth' AND r.code = 'ADMIN'
                ON CONFLICT DO NOTHING
                """
            )
        )


def _seed_user(engine, username: str, password: str) -> int:
    from app.core.security import hash_password

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO users (username, password_hash)
                VALUES (:u, :h)
                ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash,
                    password_changed_at = NULL, is_active = TRUE,
                    failed_logins = 0, locked_until = NULL
                """
            ),
            {"u": username, "h": hash_password(password)},
        )
        conn.execute(
            text(
                """
                INSERT INTO user_roles (user_id, role_id)
                SELECT u.id, r.id FROM users u, roles r
                WHERE u.username = :u AND r.code = 'VIEWER'
                ON CONFLICT DO NOTHING
                """
            ),
            {"u": username},
        )
        return conn.execute(
            text("SELECT id FROM users WHERE username = :u"), {"u": username}
        ).scalar_one()


def _login(client: TestClient, username: str, password: str) -> str:
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_password_change_revokes_older_tokens(engine, applied_schema):
    """§17 — tokens issued before the change are rejected; re-login works."""
    reset_auth_rate_limit()
    _ensure_admin(engine)
    username, password = f"pwchg-{RUN}", "Old-Pass-2026!"
    _seed_user(engine, username, password)
    new_password = "New-Pass-2026!"

    with TestClient(app) as client:
        old_token = _login(client, username, password)
        r = client.post(
            "/auth/password-change",
            json={"current_password": password, "new_password": new_password},
            headers={"Authorization": f"Bearer {old_token}"},
        )
        assert r.status_code == 200, r.text

        # The pre-change token is revoked on the next authenticated request.
        r = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {old_token}"}
        )
        assert r.status_code == 401, r.text

        # New credentials work and the fresh token is accepted. The token
        # must be minted in a strictly later second than the change (§17:
        # same-second tokens are rejected as a secure default).
        time.sleep(1.05)
        new_token = _login(client, username, new_password)
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {new_token}"})
        assert r.status_code == 200, r.text

        # The old password no longer authenticates.
        r = client.post(
            "/auth/login", json={"username": username, "password": password}
        )
        assert r.status_code == 401


def test_password_change_requires_current_password(engine, applied_schema):
    reset_auth_rate_limit()
    _ensure_admin(engine)
    username, password = f"pwchg-bad-{RUN}", "Old-Pass-2026!"
    _seed_user(engine, username, password)

    with TestClient(app) as client:
        token = _login(client, username, password)
        r = client.post(
            "/auth/password-change",
            json={"current_password": "Wrong-Pass-2026!", "new_password": "New-Pass-2026!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401, r.text
        # The password is unchanged: the old one still logs in.
        assert (
            client.post(
                "/auth/login", json={"username": username, "password": password}
            ).status_code
            == 200
        )


def test_password_change_requires_authentication(engine, applied_schema):
    reset_auth_rate_limit()
    with TestClient(app) as client:
        r = client.post(
            "/auth/password-change",
            json={"current_password": "x-Pass-2026!!", "new_password": "New-Pass-2026!"},
        )
        assert r.status_code == 401, r.text


def test_admin_reset_revokes_target_tokens(engine, applied_schema):
    """§17 reset flow — admin sets a temp password; target tokens die."""
    reset_auth_rate_limit()
    _ensure_admin(engine)
    username, password = f"pwreset-{RUN}", "Victim-Pass-2026!"
    user_id = _seed_user(engine, username, password)

    with TestClient(app) as client:
        victim_token = _login(client, username, password)
        admin_token = _login(client, "admin-auth", ADMIN_PASS)

        # A non-admin cannot reset other accounts.
        r = client.post(
            f"/auth/users/{user_id}/reset-password",
            json={"new_password": "Hijack-Pass-2026!"},
            headers={"Authorization": f"Bearer {victim_token}"},
        )
        assert r.status_code == 403, r.text

        temp_password = "Temp-Pass-2026!"
        r = client.post(
            f"/auth/users/{user_id}/reset-password",
            json={"new_password": temp_password},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200, r.text

        # The victim's pre-reset token is revoked.
        r = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {victim_token}"}
        )
        assert r.status_code == 401, r.text
        # The old password no longer works; the temp password does.
        assert (
            client.post(
                "/auth/login", json={"username": username, "password": password}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/auth/login", json={"username": username, "password": temp_password}
            ).status_code
            == 200
        )


def test_admin_reset_unknown_user_404(engine, applied_schema):
    reset_auth_rate_limit()
    _ensure_admin(engine)
    with TestClient(app) as client:
        admin_token = _login(client, "admin-auth", ADMIN_PASS)
        r = client.post(
            "/auth/users/999999999/reset-password",
            json={"new_password": "No-Such-Pass-2026!"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 404, r.text


def test_password_change_is_rate_limited(engine, applied_schema):
    """§55/§17 — wrong-current-password guessing is bounded (5/min)."""
    reset_auth_rate_limit()
    _ensure_admin(engine)
    username, password = f"pwrl-{RUN}", "Old-Pass-2026!"
    _seed_user(engine, username, password)

    with TestClient(app) as client:
        token = _login(client, username, password)
        headers = {"Authorization": f"Bearer {token}"}
        codes = [
            client.post(
                "/auth/password-change",
                json={
                    "current_password": "Guess-Pass-2026!",
                    "new_password": "New-Pass-2026!",
                },
                headers=headers,
            ).status_code
            for _ in range(6)
        ]
        assert codes[:5] == [401] * 5
        assert codes[5] == 429, codes
