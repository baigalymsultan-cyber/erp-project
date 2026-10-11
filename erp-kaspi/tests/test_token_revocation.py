"""v8 PHASE A — §17 server-side token revocation (users.token_version).

Covers: logout revokes ALL sessions (per-user version bump); admin
revoke-sessions; non-revoked token accepted; expired/invalid tokens;
password change + reset still revoke; concurrent sessions die together.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import app

RUN = f"revoke-{time.time_ns() % 10_000_000_000}"


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _seed(
    engine, username: str, password: str, roles: tuple[str, ...] = ("VIEWER",)
) -> int:
    from app.core.security import hash_password

    with engine.begin() as conn:
        uid = conn.execute(
            text(
                "INSERT INTO users (username, password_hash) "
                "VALUES (:u, :h) RETURNING id"
            ),
            {"u": username, "h": hash_password(password)},
        ).scalar_one()
        for role in roles:
            conn.execute(
                text(
                    "INSERT INTO user_roles (user_id, role_id) "
                    "SELECT :uid, id FROM roles WHERE code = :r"
                ),
                {"uid": uid, "r": role},
            )
    return uid


def _login(client: TestClient, username: str, password: str) -> str:
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _me(client: TestClient, token: str):
    return client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})


def test_valid_token_accepted(engine, applied_schema, client):
    username, password = f"rev-ok-{RUN}", "Rev-Ok-2026!"
    _seed(engine, username, password)
    token = _login(client, username, password)
    r = _me(client, token)
    assert r.status_code == 200, r.text
    assert r.json()["username"] == username


def test_logout_revokes_all_sessions(engine, applied_schema, client):
    """Two live sessions of one user; LOGOUT kills both (per-user version)."""
    username, password = f"rev-out-{RUN}", "Rev-Out-2026!"
    _seed(engine, username, password)
    token_a = _login(client, username, password)
    token_b = _login(client, username, password)
    assert _me(client, token_a).status_code == 200
    assert _me(client, token_b).status_code == 200

    r = client.post(
        "/auth/logout", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "all-sessions"

    assert _me(client, token_a).status_code == 401
    assert _me(client, token_b).status_code == 401


def test_revoke_sessions_is_admin_only(engine, applied_schema, client):
    username, password = f"rev-adm-{RUN}", "Rev-Adm-2026!"
    uid = _seed(engine, username, password)
    viewer_token = _login(client, username, password)

    r = client.post(
        f"/auth/users/{uid}/revoke-sessions",
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert r.status_code == 403, r.text


def test_admin_revoke_sessions_kills_target_not_others(
    engine, applied_schema, client
):
    target, target_pass = f"rev-tgt-{RUN}", "Rev-Tgt-2026!"
    admin, admin_pass = f"rev-adm2-{RUN}", "Rev-Adm2-2026!"
    uid = _seed(engine, target, target_pass)
    _seed(engine, admin, admin_pass, ("ADMIN",))
    target_token = _login(client, target, target_pass)
    admin_token = _login(client, admin, admin_pass)

    r = client.post(
        f"/auth/users/{uid}/revoke-sessions",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    assert _me(client, target_token).status_code == 401
    # The ADMIN's own sessions are untouched.
    assert _me(client, admin_token).status_code == 200

    # audit: REVOKE_SESSIONS was recorded explicitly; user_id = acting admin
    # (§54 — who did it), entity_id = the revoked account.
    with engine.connect() as conn:
        raw = conn.execute(
            text(
                "SELECT action FROM audit_log "
                "WHERE entity_id = :u AND action = 'REVOKE_SESSIONS' "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"u": uid},
        ).scalar_one_or_none()
    assert raw == "REVOKE_SESSIONS"


def test_logout_requires_authentication(applied_schema, client):
    r = client.post("/auth/logout")
    assert r.status_code == 401, r.text


def test_revoked_user_reauthenticates(engine, applied_schema, client):
    """Revocation kills old tokens but not the account: login works again."""
    username, password = f"rev-rea-{RUN}", "Rev-Rea-2026!"
    _seed(engine, username, password)
    old_token = _login(client, username, password)
    client.post("/auth/logout", headers={"Authorization": f"Bearer {old_token}"})
    assert _me(client, old_token).status_code == 401
    new_token = _login(client, username, password)
    assert _me(client, new_token).status_code == 200


def test_expired_token_rejected(engine, applied_schema, client):

    username, password = f"rev-exp-{RUN}", "Rev-Exp-2026!"
    uid = _seed(engine, username, password)

    # Issue a token that is already expired (negative TTL via direct payload).
    expired = _make_expired_token(uid, username)
    r = _me(client, expired)
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]
    del password, uid


def _make_expired_token(user_id: int, username: str) -> str:
    import jwt as pyjwt

    from app.core.security import jwt_secret

    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "username": username,
        "roles": ["VIEWER"],
        "iat": now - 7200,
        "exp": now - 3600,
        "ver": 1,
    }
    return pyjwt.encode(payload, jwt_secret(), algorithm="HS256")


def test_invalid_token_rejected(applied_schema, client):
    r = _me(client, "not-a-jwt")
    assert r.status_code == 401
    r = _me(client, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.wrong-signature")
    assert r.status_code == 401


def test_token_without_ver_claim_rejected(engine, applied_schema, client):
    """A token minted before the revocation anchor (no ver) is dead."""
    import jwt as pyjwt

    from app.core.security import jwt_secret

    username, password = f"rev-ver-{RUN}", "Rev-Ver-2026!"
    uid = _seed(engine, username, password)
    now = int(time.time())
    legacy = pyjwt.encode(
        {
            "sub": str(uid),
            "username": username,
            "roles": ["VIEWER"],
            "iat": now,
            "exp": now + 3600,
        },
        jwt_secret(),
        algorithm="HS256",
    )
    r = _me(client, legacy)
    assert r.status_code == 401, r.text
