"""
API auth tests — /auth endpoints against the real PostgreSQL schema (§52–§55).

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
ADMIN_PASS = "Director-Pass-2026"


def _ensure_admin(engine) -> None:
    """Create the ADMIN bootstrap user if this run has not made it yet."""
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


def test_auth_flow_admin(engine, applied_schema):
    """Login → /auth/me → create user (ADMIN privilege enforced)."""
    _ensure_admin(engine)
    with TestClient(app) as client:
        r = client.post(
            "/auth/login",
            json={"username": "admin-auth", "password": ADMIN_PASS},
        )
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        assert r.json()["token_type"] == "bearer"
        assert "ADMIN" in r.json()["roles"]
        auth = {"Authorization": f"Bearer {token}"}

        r = client.get("/auth/me", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["username"] == "admin-auth"

        # unauthenticated access is rejected
        assert client.get("/auth/me").status_code == 401
        # garbage token is rejected
        assert client.get("/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401

        r = client.post(
            "/auth/users",
            headers=auth,
            json={
                "username": f"viewer-{RUN}",
                "password": "Viewer-Pass-123",
                "roles": ["VIEWER"],
            },
        )
        assert r.status_code == 201, r.text

        # duplicate username → 409
        r = client.post(
            "/auth/users",
            headers=auth,
            json={
                "username": f"viewer-{RUN}",
                "password": "Viewer-Pass-123",
                "roles": ["VIEWER"],
            },
        )
        assert r.status_code == 409

        # unknown role → 422
        r = client.post(
            "/auth/users",
            headers=auth,
            json={
                "username": f"ghost-{RUN}",
                "password": "Ghost-Pass-1234",
                "roles": ["NOT_A_ROLE"],
            },
        )
        assert r.status_code == 422


def test_auth_viewers_role_list_is_scoped(engine, applied_schema):
    """REGRESSION (§52): user_roles() must return only the user's own roles."""
    from app.core.security import hash_password

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO users (username, password_hash)
                VALUES (:u, :h) ON CONFLICT (username) DO NOTHING
                """
            ),
            {"u": f"plain-{RUN}", "h": hash_password("Plain-Pass-1234")},
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
            {"u": f"plain-{RUN}"},
        )

    with TestClient(app) as client:
        r = client.post(
            "/auth/login",
            json={"username": f"plain-{RUN}", "password": "Plain-Pass-1234"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["roles"] == ["VIEWER"], "a single-role user must not inherit every role"

        # VIEWER must not be able to create users (§53)
        token = r.json()["access_token"]
        r = client.post(
            "/auth/users",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "username": f"elevated-{RUN}",
                "password": "Evil-Pass-12345",
                "roles": ["ADMIN"],
            },
        )
        assert r.status_code == 403, r.text


def test_login_rejects_bad_password(engine, applied_schema):
    _ensure_admin(engine)
    with TestClient(app) as client:
        r = client.post(
            "/auth/login",
            json={"username": "admin-auth", "password": "wrong-password"},
        )
        assert r.status_code == 401
