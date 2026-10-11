"""
Login gate for the Streamlit UI (TD-014, §52–§55) — TD-016 P4.

All authentication paths go through the API boundary (frontend/api_client):
the UI no longer opens a database session at all. One behaviour, one entry
point: the same POST /auth/login that any HTTP client gets (bcrypt verify,
lockout, failed-login counter, is_active), the same GET /auth/me revalidation
on every rerun, the same POST /auth/logout server-side revocation (§17).

Audit rows (LOGIN / LOGIN_FAILED / LOGOUT) are written server-side by the
API and its fn_audit() triggers (§54) — the UI cannot and must not write to
audit_log directly any more.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from app.core.security import LOCKOUT_MINUTES, LOCKOUT_THRESHOLD, ROLE_PRIVILEGE
from frontend import api_client

#: session_state keys (single definition — no scattered magic strings).
_AUTH_KEY = "auth_user"
_ALLOWED_IPS_KEY = "allowed_ip_ids"

#: §53 hierarchy — single source of truth lives in app.core.security.
_ROLE_PRIVILEGE = ROLE_PRIVILEGE


class AuthError(RuntimeError):
    """Authentication failed; ``kind`` selects the user-facing message."""


def _login_error(status_code: int, detail: str) -> AuthError:
    """Map the API's uniform status codes to UI message kinds (§53)."""
    if status_code == 423:
        return AuthError("locked")
    if status_code == 403:
        return AuthError("inactive")
    if status_code == 429:
        return AuthError("rate_limited")
    return AuthError("invalid")


def authenticate(username: str, password: str) -> dict[str, Any]:
    """Validate credentials via POST /auth/login; return the session payload.

    Raises AuthError with kind ``locked`` | ``invalid`` | ``inactive`` |
    ``rate_limited`` — the caller renders one generic message for ``invalid``
    (no user enumeration, same uniform-401 reasoning as the API).
    The JWT is stored only in st.session_state (never on disk).
    """
    username = username.strip()
    if not username or not password:
        raise AuthError("invalid")

    try:
        response = api_client.request(
            "POST",
            "/auth/login",
            json={"username": username, "password": password},
        )
    except api_client.ApiError as exc:
        raise _login_error(exc.status_code, exc.kind) from exc

    token = str(response["access_token"])
    api_client.store_token(token)
    # TokenResponse carries only the JWT + roles; the session payload (and the
    # ИП allow-list) comes from /auth/me, which also proves the token works.
    me = api_client.get("/auth/me", token=token)
    allowed = me.get("allowed_ip_ids")
    st.session_state[_ALLOWED_IPS_KEY] = (
        True if allowed is None else [int(i) for i in allowed]
    )
    payload: dict[str, Any] = {
        "user_id": int(me["user_id"]),
        "username": str(me["username"]),
        "full_name": me.get("full_name"),
        "roles": sorted(str(r) for r in me.get("roles", [])),
    }
    st.session_state[_AUTH_KEY] = payload
    return payload


def current_auth() -> dict[str, Any] | None:
    """The authenticated session payload, or None.

    MASTER PROMPT §12/§17 — the cached payload is never trusted blindly:
    on every rerun GET /auth/me revalidates the JWT server-side, so a
    disabled or deleted account, a revoked token (password change, logout,
    admin revoke-sessions) or a role change takes effect on the next rerun.
    Any 401/403 from /auth/me logs the UI session out.
    """
    token = api_client.current_token()
    if token is None:
        return None
    try:
        me = api_client.get("/auth/me", token=token)
    except api_client.ApiError as exc:
        if exc.status_code in (401, 403):
            discard_session()
            return None
        raise
    payload: dict[str, Any] = {
        "user_id": int(me["user_id"]),
        "username": str(me["username"]),
        "full_name": me.get("full_name"),
        "roles": sorted(str(r) for r in me.get("roles", [])),
    }
    st.session_state[_AUTH_KEY] = payload
    allowed = me.get("allowed_ip_ids")
    st.session_state[_ALLOWED_IPS_KEY] = (
        True if allowed is None else [int(i) for i in allowed]
    )
    return payload


def discard_session() -> None:
    st.session_state.pop(_AUTH_KEY, None)
    st.session_state.pop(_ALLOWED_IPS_KEY, None)
    api_client.clear_token()


def logout() -> None:
    """POST /auth/logout — the server bumps the token version (§17), so the
    JWT stops working on every device; then clear the UI session."""
    token = api_client.current_token()
    if token is not None:
        try:
            api_client.request("POST", "/auth/logout", token=token)
        except api_client.ApiError as exc:
            if exc.status_code not in (401, 403):
                raise
    discard_session()


def _allowed_ip_ids() -> list[int] | None:
    """The user's ИП allow-list from /auth/me (None = unrestricted).

    Cached for the current rerun; current_auth() refreshes it every rerun.
    """
    value = st.session_state.get(_ALLOWED_IPS_KEY)
    if value is True:
        return None  # unrestricted (ADMIN/DIRECTOR)
    if isinstance(value, list):
        return [int(i) for i in value]
    auth = current_auth()
    if auth is None:
        return []
    cached = st.session_state.get(_ALLOWED_IPS_KEY)
    if cached is True:
        return None
    if isinstance(cached, list):
        return [int(i) for i in cached]
    return []


def restrict_ip_choices(rows: list[Any]) -> list[Any]:
    """Restrict the ИП filter rows to the user's allow-list (API1).

    ``rows`` are the selectable ИП rows (without the leading "all" marker);
    the return value re-adds the "all" marker (None) only when the user is
    unrestricted (ADMIN/DIRECTOR). A user with an empty allow-list gets an
    empty list — the UI shows the same refusal the API returns (403, no
    object enumeration).
    """
    auth = current_auth()
    if auth is None:
        return []
    allowed = _allowed_ip_ids()
    if allowed is None:
        return [None, *rows]
    ids = set(allowed)
    return [row for row in rows if getattr(row, "id", row) in ids]


def has_min_role(auth: dict[str, Any], minimum: str) -> bool:
    """§53 hierarchy check — does the user hold at least ``minimum``?"""
    floor = _ROLE_PRIVILEGE.get(minimum)
    if floor is None:
        return False
    return any(_ROLE_PRIVILEGE.get(r, -1) >= floor for r in auth["roles"])


_LOGIN_CSS = """
<style>
    .login-wrap {max-width: 420px; margin: 8vh auto 0 auto;}
    .login-card {background: var(--surface); border: 1px solid var(--line);
        border-radius: 16px; padding: 2rem 2.2rem;}
    .login-brand {font-size: 2.2rem; text-align: center; margin-bottom: .2rem;}
    .login-title {text-align: center; font-weight: 700; font-size: 1.1rem;
        color: var(--ink); margin-bottom: 1.4rem;}
    .login-foot {text-align: center; color: var(--muted); font-size: .78rem;
        margin-top: 1.2rem;}
</style>
"""


def login_gate(theme_css: str) -> None:
    """Render the login screen until authenticate() succeeds (TD-014).

    Localization comes from the single I18N catalog in frontend.app (RU/KZ,
    §58); deferred import breaks the app→auth_gate import cycle (frontend.app
    imports this module at top-level, so ``I18N`` only resolves at call time).
    The screen offers the same RU/KZ switch as the sidebar (§58), `lang`
    defaults to RU (§10) and persists via session_state["lang"].
    """
    from frontend.app import I18N

    lang = st.radio(
        "🌐",
        ["ru", "kk"],
        format_func=lambda v: "Русский" if v == "ru" else "Қазақша",
        key="lang",
        horizontal=True,
    )
    t = I18N[lang]
    st.markdown(theme_css, unsafe_allow_html=True)
    st.markdown(_LOGIN_CSS, unsafe_allow_html=True)

    st.markdown(
        f"""
        <div class="login-wrap"><div class="login-card">
            <div class="login-brand">⢕</div>
            <div class="login-title">{t["login_title"]}</div>
        </div></div>
        """,
        unsafe_allow_html=True,
    )

    with st.container():
        st.markdown('<div class="login-wrap">', unsafe_allow_html=True)
        username = st.text_input(t["login_username"], key="login_username",
                                 autocomplete="username")
        password = st.text_input(t["login_password"], type="password",
                                 key="login_password",
                                 autocomplete="current-password")
        submitted = st.button(t["login_submit"], type="primary",
                              use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    if not submitted:
        st.stop()
    try:
        authenticate(username, password)
    except AuthError as exc:
        if str(exc) == "locked":
            st.error(
                t["err_locked"].format(n=LOCKOUT_THRESHOLD, m=LOCKOUT_MINUTES)
            )
        elif str(exc) == "inactive":
            st.error(t["err_inactive"])
        elif str(exc) == "rate_limited":
            st.error(t["err_rate_limited"])
        else:
            st.error(t["err_invalid"])
        st.stop()

    st.rerun()


def sidebar_user_block(lang: str) -> None:
    """Sidebar badge: user, roles, logout. Called after the gate passed."""
    from frontend.app import I18N

    t = I18N[lang]
    auth = current_auth()
    if auth is None:
        return
    who = auth["full_name"] or auth["username"]
    st.sidebar.markdown(
        f'<div class="erp-chip">👤 {who}</div>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        '<div style="display:flex;gap:.3rem;flex-wrap:wrap;margin:.2rem 0 .6rem 0;">'
        + "".join(
            f'<span class="erp-chip erp-chip-ok">{r}</span>' for r in auth["roles"]
        )
        + "</div>",
        unsafe_allow_html=True,
    )
    if st.sidebar.button(t["logout"], use_container_width=True, key="btn_logout"):
        logout()
        st.rerun()
