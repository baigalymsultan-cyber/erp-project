"""
TD-016 P4 — login gate via the API boundary (TD-014 semantics preserved).

The UI no longer talks to the database; every assertion below therefore
goes through the real FastAPI handlers (in-process ASGI transport) and, for
state checks, through the test database directly (§94 — evidence, not mocks):

  * authenticate()  → POST /auth/login: 401/403/423/429 mapped to AuthError
    kinds; JWT stored only in st.session_state;
  * current_auth()  → GET /auth/me revalidation on every call: deactivation,
    deletion, role change and token revocation log the UI session out;
  * logout()        → POST /auth/logout: server-side token-version bump —
    the stored JWT stops working (§17);
  * LOGIN / LOGIN_FAILED / LOGOUT audit rows are written server-side (§54);
  * has_min_role() / restrict_ip_choices() — pure UI logic over the
    allow-list the server computed.
"""

from __future__ import annotations

import uuid

import pytest
import streamlit as st
from sqlalchemy import Engine, text

from frontend import api_client
from frontend.auth_gate import (
    AuthError,
    authenticate,
    current_auth,
    has_min_role,
    logout,
    restrict_ip_choices,
)

RUN = uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    """Each test gets a clean auth-rate-limit state (§55 bucket reset)."""
    from app.core.security import reset_auth_rate_limit

    reset_auth_rate_limit()
    yield
    reset_auth_rate_limit()


@pytest.fixture()
def clean_auth_state():
    """Guarantee a pristine (unauthenticated) session_state per test."""
    st.session_state.pop("auth_user", None)
    api_client.clear_token()
    yield
    st.session_state.pop("auth_user", None)
    api_client.clear_token()


def _seed_user(engine: Engine, username: str, password: str, roles: tuple[str, ...],
               *, is_active: bool = True, employee_ip: int | None = None) -> int:
    from app.core.security import hash_password

    with engine.begin() as conn:
        uid = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, is_active) "
                "VALUES (:u, :h, :a) RETURNING id"
            ),
            {"u": username, "h": hash_password(password), "a": is_active},
        ).scalar_one()
        for role in roles:
            conn.execute(
                text(
                    "INSERT INTO user_roles (user_id, role_id) "
                    "SELECT :u, id FROM roles WHERE code = :r"
                ),
                {"u": uid, "r": role},
            )
        if employee_ip is not None:
            emp = conn.execute(
                text(
                    "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
                    "VALUES (:n, :ip, 'PIECEWORK') RETURNING id"
                ),
                {"n": f"Gate Emp {RUN}", "ip": employee_ip},
            ).scalar_one()
            conn.execute(
                text("UPDATE users SET employee_id = :e WHERE id = :u"),
                {"e": emp, "u": uid},
            )
    return uid


def _seed_ip(engine: Engine) -> int:
    name, bin_ = f"Gate IP {RUN}", f"96{int(RUN, 16) % 100_000_000:09d}0"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) VALUES (:n, :b)"
                " ON CONFLICT (bin) DO NOTHING"
            ),
            {"n": name, "b": bin_},
        )
        return conn.execute(
            text("SELECT id FROM ip_entities WHERE bin = :b"), {"b": bin_}
        ).scalar_one()


def _set_stale_session(user_id: int, username: str, roles: list[str]) -> None:
    """Simulate a payload cached by an old rerun (never trusted by the gate)."""
    st.session_state["auth_user"] = {
        "user_id": user_id, "username": username, "full_name": None, "roles": roles,
    }


# --------------------------------------------------------------------------- #
# authenticate()
# --------------------------------------------------------------------------- #


def test_authenticate_success_writes_audit(engine, applied_schema, clean_auth_state):
    username, password = f"gate-ok-{RUN}", "Gate-Pass-2026!"
    _seed_user(engine, username, password, ("VIEWER",))

    auth = authenticate(username, password)

    assert auth["username"] == username
    assert auth["roles"] == ["VIEWER"]
    assert isinstance(api_client.current_token(), str)
    with engine.begin() as conn:
        action = conn.execute(
            text(
                "SELECT a.action FROM audit_log a JOIN users u ON u.id = a.user_id "
                "WHERE u.username = :u ORDER BY a.id DESC LIMIT 1"
            ),
            {"u": username},
        ).scalar_one()
    assert action == "LOGIN"


def test_authenticate_wrong_password_counts_and_audits(engine, applied_schema, clean_auth_state):
    username, password = f"gate-bad-{RUN}", "Gate-Pass-2026!"
    _seed_user(engine, username, password, ("VIEWER",))

    with pytest.raises(AuthError) as exc:
        authenticate(username, "Wrong-Pass-2026!")
    assert str(exc.value) == "invalid"

    with engine.begin() as conn:
        failed, locked = conn.execute(
            text("SELECT failed_logins, locked_until FROM users WHERE username = :u"),
            {"u": username},
        ).one()
    assert failed == 1
    assert locked is None
    with engine.begin() as conn:
        action = conn.execute(
            text(
                "SELECT a.action FROM audit_log a JOIN users u ON u.id = a.user_id "
                "WHERE u.username = :u ORDER BY a.id DESC LIMIT 1"
            ),
            {"u": username},
        ).scalar_one()
    assert action == "LOGIN_FAILED"


def test_lockout_after_threshold(engine, applied_schema, clean_auth_state):
    username, password = f"gate-lock-{RUN}", "Gate-Pass-2026!"
    _seed_user(engine, username, password, ("VIEWER",))

    for _ in range(5):
        with pytest.raises(AuthError) as exc:
            authenticate(username, "Wrong-Pass-2026!")
        assert str(exc.value) == "invalid"

    with engine.begin() as conn:
        locked_until = conn.execute(
            text("SELECT locked_until FROM users WHERE username = :u"),
            {"u": username},
        ).scalar_one()
    assert locked_until is not None

    with pytest.raises(AuthError) as exc:
        authenticate(username, password)
    assert str(exc.value) in {"locked", "rate_limited"}


def test_authenticate_inactive(engine, applied_schema, clean_auth_state):
    username, password = f"gate-off-{RUN}", "Gate-Pass-2026!"
    _seed_user(engine, username, password, ("VIEWER",), is_active=False)

    with pytest.raises(AuthError) as exc:
        authenticate(username, password)
    assert str(exc.value) == "inactive"


def test_no_user_enumeration(engine, applied_schema, clean_auth_state):
    with pytest.raises(AuthError) as exc:
        authenticate(f"gate-ghost-{RUN}", "No-Such-User-2026!")
    assert str(exc.value) == "invalid"


def test_empty_credentials_rejected_locally(engine, applied_schema, clean_auth_state):
    with pytest.raises(AuthError) as exc:
        authenticate("   ", "")
    assert str(exc.value) == "invalid"


# --------------------------------------------------------------------------- #
# current_auth() — per-rerun revalidation
# --------------------------------------------------------------------------- #


def test_current_auth_revalidates_user_state(engine, applied_schema, clean_auth_state):
    username, password = f"gate-reval-{RUN}", "Gate-Pass-2026!"
    uid = _seed_user(engine, username, password, ("VIEWER",))

    authenticate(username, password)
    auth = current_auth()
    assert auth is not None and auth["roles"] == ["VIEWER"]

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET is_active = FALSE WHERE id = :u"), {"u": uid}
        )
    assert current_auth() is None
    assert api_client.current_token() is None


def test_current_auth_refreshes_roles(engine, applied_schema, clean_auth_state):
    username, password = f"gate-roles-{RUN}", "Gate-Pass-2026!"
    uid = _seed_user(engine, username, password, ("VIEWER",))

    authenticate(username, password)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT :u, id FROM roles WHERE code = 'ACCOUNTANT'"
            ),
            {"u": uid},
        )
    auth = current_auth()
    assert auth is not None
    assert auth["roles"] == ["ACCOUNTANT", "VIEWER"]


def test_current_auth_deleted_user_logs_out(engine, applied_schema, clean_auth_state):
    username, password = f"gate-del-{RUN}", "Gate-Pass-2026!"
    uid = _seed_user(engine, username, password, ("VIEWER",))

    authenticate(username, password)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    assert current_auth() is None


def test_current_auth_revoked_token_logs_out(engine, applied_schema, clean_auth_state):
    username, password = f"gate-revoked-{RUN}", "Gate-Pass-2026!"
    uid = _seed_user(engine, username, password, ("VIEWER",))

    authenticate(username, password)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE users SET token_version = token_version + 1 WHERE id = :u"
            ),
            {"u": uid},
        )
    assert current_auth() is None
    assert api_client.current_token() is None


def test_current_auth_without_token_is_none(applied_schema, clean_auth_state):
    _set_stale_session(12345, "ghost", ["VIEWER"])
    assert current_auth() is None


def test_logout_revokes_token_server_side(engine, applied_schema, clean_auth_state):
    from app.core.security import decode_token

    username, password = f"gate-lo-{RUN}", "Gate-Pass-2026!"
    uid = _seed_user(engine, username, password, ("VIEWER",))

    authenticate(username, password)
    token_before = api_client.current_token()
    assert token_before is not None
    assert decode_token(token_before) is not None

    logout()

    assert api_client.current_token() is None
    assert current_auth() is None
    with engine.begin() as conn:
        version = conn.execute(
            text("SELECT token_version FROM users WHERE id = :u"), {"u": uid}
        ).scalar_one()
    assert version >= 2


# --------------------------------------------------------------------------- #
# has_min_role() — §53 hierarchy (pure UI logic)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("roles", "minimum", "expected"),
    [
        (("VIEWER",), "VIEWER", True),
        (("VIEWER",), "HR", False),
        (("HR",), "HR", True),
        (("BRIGADIER",), "HR", False),
        (("HR", "SHOP_MANAGER"), "HR", True),
        (("ACCOUNTANT",), "DIRECTOR", False),
        (("DIRECTOR",), "DIRECTOR", True),
        (("ADMIN",), "DIRECTOR", True),
        ((), "VIEWER", False),
    ],
)
def test_has_min_role(roles, minimum, expected):
    auth = {"user_id": 1, "username": "t", "full_name": None, "roles": list(roles)}
    assert has_min_role(auth, minimum) is expected


# --------------------------------------------------------------------------- #
# restrict_ip_choices() — OWASP API1 over the server-computed allow-list
# --------------------------------------------------------------------------- #


class _IpRow:
    def __init__(self, id: int, name: str) -> None:
        self.id = id
        self.name = name


def test_restrict_ip_choices_unrestricted_admin(engine, applied_schema, clean_auth_state):
    ip_id = _seed_ip(engine)
    username, password = f"gate-ipadm-{RUN}", "Gate-Pass-2026!"
    _seed_user(engine, username, password, ("ADMIN",))

    authenticate(username, password)
    rows = [_IpRow(ip_id, "ИП-1")]
    assert restrict_ip_choices(rows) == [None, *rows]


def test_restrict_ip_choices_restricted_user(engine, applied_schema, clean_auth_state):
    ip1 = _seed_ip(engine)
    username, password = f"gate-ipusr-{RUN}", "Gate-Pass-2026!"
    _seed_user(engine, username, password, ("VIEWER",), employee_ip=ip1)

    authenticate(username, password)
    rows = [_IpRow(ip1, "ИП-1"), _IpRow(ip1 + 100000, "ИП-чужой")]
    assert restrict_ip_choices(rows) == [rows[0]]


def test_restrict_ip_choices_no_allow_list_empty(engine, applied_schema, clean_auth_state):
    username, password = f"gate-ipnone-{RUN}", "Gate-Pass-2026!"
    _seed_user(engine, username, password, ("VIEWER",))

    authenticate(username, password)
    assert restrict_ip_choices([_IpRow(1, "ИП-1")]) == []


def test_restrict_ip_choices_unauthenticated(engine, applied_schema, clean_auth_state):
    assert restrict_ip_choices([_IpRow(1, "ИП-1")]) == []
