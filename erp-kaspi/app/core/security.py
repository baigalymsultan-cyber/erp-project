"""
§52/§53/§55 — Authentication and RBAC enforcement for the FastAPI layer.

Password hashing is bcrypt (§55: only a secure hash is stored — the
``chk_users_password_hash`` CHECK in the schema rejects anything else).
Sessions are stateless JWTs (HS256) signed with ``APP_JWT_SECRET`` from the
environment (§10: secrets never live in code). Role checks happen in the
backend on every protected endpoint — never only in the UI (§53).

Rate limiting of the auth endpoints (§55) is enforced with a sliding window
whose state lives in PostgreSQL when ``RATE_LIMIT_SHARED=1`` (TD-015) and in
process memory otherwise; the in-process window remains as the per-worker
fast path and as the fail-open fallback when the shared store is down.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.auth import Role, User, UserRole
from app.core.logging import bind_request_context
from app.core.rate_limit_store import (
    clear_all as _store_clear_all,
)
from app.core.rate_limit_store import (
    count_recent as _store_count_recent,
)
from app.core.rate_limit_store import (
    is_persistent_error as _store_is_persistent_error,
)
from app.core.rate_limit_store import (
    lock_bucket as _store_lock_bucket,
)
from app.core.rate_limit_store import (
    prune as _store_prune,
)
from app.core.rate_limit_store import (
    record as _store_record,
)

#: Roles in the order of privilege; index comparison implements the §53 hierarchy.
ROLE_PRIVILEGE: dict[str, int] = {  #: §53 hierarchy — single source of truth.
    "VIEWER": 0,
    "BRIGADIER": 1,
    "SHOP_MANAGER": 2,
    "HR": 2,
    "ACCOUNTANT": 3,
    "DIRECTOR": 4,
    "ADMIN": 5,
}

#: Bearer-scheme extractor; auto_error=False so we control the error shape.
_bearer = HTTPBearer(auto_error=False)

#: Sliding window for the auth endpoints (§55): 5 attempts per 60 seconds.
_AUTH_WINDOW_SECONDS = 60
_AUTH_MAX_ATTEMPTS = 5
#: prune pass trigger — the rate-limit map never grows past this + 1 window
_AUTH_PRUNE_THRESHOLD = 10_000
_auth_attempts: dict[str, deque[float]] = defaultdict(deque)

#: TD-015 — shared (multi-worker) rate limiting. RATE_LIMIT_SHARED=1 routes
#: every auth attempt through the ``auth_rate_events`` table so the §55
#: window holds across workers/replicas. Default is off: single-process
#: deployments and the test suite keep the in-process window only.
_RATE_LIMIT_SHARED = os.environ.get("RATE_LIMIT_SHARED", "0") == "1"

#: §7 deployment invariant — production => shared limiter mandatory.
#: A multi-worker/replica deployment with only per-process windows has no
#: real rate limit: each worker sees 1/N of the attempts. Fail fast at
#: startup instead of silently running unprotected.
_APP_ENV = os.environ.get("APP_ENV", "").strip().lower()
if _APP_ENV in ("production", "prod") and not _RATE_LIMIT_SHARED:
    raise RuntimeError(
        "APP_ENV=production requires RATE_LIMIT_SHARED=1: per-process rate "
        "limits do not hold across workers/replicas. Set RATE_LIMIT_SHARED=1 "
        "(and ensure the auth_rate_events table is migrated) or do not run "
        "with APP_ENV=production."
    )

#: Shared-store attempt budget: give the INSERT/SELECT at most this long
#: before falling back to the local window (fail-open, ADR-014). A rate
#: limiter must never become the thing that takes login down.
_SHARED_TIMEOUT_SECONDS = 0.5

#: Per-statement bounds for the shared-store session. lock_timeout stops a
#: contended (or stuck) bucket from turning login into a 30s stall — the
#: default statement_timeout alone would let the advisory lock block far past
#: the fail-open deadline; statement_timeout caps COUNT+INSERT+prune so a slow
#: store raises a SQLAlchemyError and degrades to the per-worker window.
_SHARED_STATEMENT_TIMEOUT_MS = 700
_SHARED_LOCK_TIMEOUT_MS = 600

#: Probability that one call ALSO runs an opportunistic prune (DELETE of
#: expired rows), in addition to the mandatory INSERT of the attempt. The
#: INSERT is unconditional so every attempt is counted exactly once; the
#: prune merely bounds table growth without a background worker.
_SHARED_PRUNE_PROBABILITY = 0.05

logger = logging.getLogger("erp.security")


def _shared_session_scope() -> Any:
    """Short-lived transactional session for the shared store, imported
    lazily so the module import graph stays acyclic. session_scope()
    commits on success and rolls back on error (app.core.database)."""
    from app.core.database import session_scope

    return session_scope()


#: JWT lifetime in seconds.
_TOKEN_TTL_SECONDS = 8 * 60 * 60


class JwtSecretMissing(RuntimeError):
    """APP_JWT_SECRET is not configured — auth cannot run (§10)."""


def jwt_secret() -> str:
    """The signing secret, read from the environment at call time (§10)."""
    import os

    value = os.environ.get("APP_JWT_SECRET", "")
    if len(value) < 32:
        raise JwtSecretMissing("APP_JWT_SECRET must be set to at least 32 characters (§10)")
    return value


# --------------------------------------------------------------------------- #
# Passwords — §55
# --------------------------------------------------------------------------- #


#: Failed logins before the account is temporarily locked (§55, audit #9).
#: Single source of truth — the API layer and the Streamlit login gate both
#: import these values so the semantics cannot drift apart (TD-014).
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15


def hash_password(plaintext: str) -> str:
    """bcrypt hash with a per-password salt; the result satisfies the CHECK."""
    if len(plaintext) < 8:
        raise ValueError("password must be at least 8 characters")
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plaintext: str, password_hash: str) -> bool:
    """Constant-time verification; never raises on a malformed hash."""
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #


def issue_token(user: User, roles: list[str]) -> str:
    """A signed JWT naming the user and her roles (§52/§55)."""
    now = int(time.time())
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "roles": roles,
        "ver": int(getattr(user, "token_version", 1) or 1),
        "iat": now,
        "exp": now + _TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, jwt_secret(), algorithm="HS256")


def decode_token(token: str) -> dict[str, object]:
    """Verify signature and expiry; raises HTTPException 401 on any problem."""
    try:
        return jwt.decode(token, jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc


# --------------------------------------------------------------------------- #
# Dependencies — §53: backend enforcement, never UI-only
# --------------------------------------------------------------------------- #


def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> dict[str, object]:
    """Resolve the bearer token to a principal with its roles.

    §53/§55 (baseline audit item 7) — the roles are re-read from the database
    on every request instead of trusting the JWT claims: a role revoked or
    granted mid-token-lifetime takes effect immediately, and a deleted or
    deactivated user loses access even though the signed token is still
    unexpired. The token signature still authenticates the identity; only the
    *authorization* data is refreshed. One indexed query per request — the
    deployment is single-process and the table is tiny.
    """
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    claims = decode_token(credentials.credentials)
    token_roles = claims.get("roles", [])
    if not isinstance(token_roles, list):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed token")
    user_id = claims["sub"]
    assert isinstance(user_id, (int, str))
    # §71 — the user id travels in every log line of this request.
    # Sync dependencies run in the threadpool, where a ContextVar change does
    # not reach the ASGI task that logs the request — so the id also rides the
    # request scope (shared mutable state), and the middleware reads it there.
    request.state.user_id = int(user_id)
    bind_request_context(user_id=int(user_id))

    # Fresh authorization data: one short-lived session, roles only.
    from app.core.database import get_session_factory

    with get_session_factory()() as session:
        row = session.execute(
            select(User.is_active, User.id, User.password_changed_at, User.token_version).where(
                User.id == int(user_id)
            )
        ).first()
        if row is None or not row.is_active:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "account disabled or deleted")
        # §17 — password-change invalidation: a token issued at or before the
        # last password change (self-service change or admin reset) is
        # revoked. Sub-second comparison: iat has second precision, so a
        # token minted in the same wall-clock second as the change is also
        # rejected (secure default; the client simply re-authenticates).
        password_changed_at = row[2]
        if password_changed_at is not None:
            iat = claims.get("iat")
            if not isinstance(iat, (int, float)) or iat <= password_changed_at.timestamp():
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "token revoked — password changed"
                )
        fresh_roles = user_roles(session, int(user_id))
        # §17 — server-side token revocation: a token issued before the
        # user's token_version was bumped (logout / admin revoke) is dead.
        token_ver = claims.get("ver")
        if not isinstance(token_ver, (int, str)) or int(token_ver) != int(
            row.token_version or 1
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token revoked")

    return {
        "user_id": int(user_id),
        "username": claims.get("username", ""),
        "roles": fresh_roles,
    }


Principal = Annotated[dict[str, object], Depends(current_principal)]


def require_role(*allowed: str):
    """A dependency factory: the principal must hold one of `allowed` (§53).

    ADMIN always passes. The check runs in the backend on every request.
    """

    def dependency(
        principal: Annotated[dict[str, object], Depends(current_principal)],
    ) -> dict[str, object]:
        role_list = cast("list[str]", principal["roles"])
        roles = set(role_list)
        if "ADMIN" in roles or roles & set(allowed):
            return principal
        raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")

    return dependency


def require_privilege(minimum: str):
    """A dependency factory: the principal's top role must reach `minimum` (§53)."""
    threshold = ROLE_PRIVILEGE[minimum]

    def dependency(
        principal: Annotated[dict[str, object], Depends(current_principal)],
    ) -> dict[str, object]:
        role_list = cast("list[str]", principal["roles"])
        if any(ROLE_PRIVILEGE.get(r, -1) >= threshold for r in role_list):
            return principal
        raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")

    return dependency


def user_roles(session: Session, user_id: int) -> list[str]:
    """The role codes held by a user (§52).

    The join must be explicit on `user_roles.role_id == roles.id` and filtered
    on `user_roles.user_id == user_id`: relying on relationship inference
    produced a cartesian FROM (users JOIN user_roles, roles) without a
    user-side filter on the role rows, so every user received every role —
    a privilege-escalation hole (§53).
    """
    stmt = (
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    return list(session.scalars(stmt))


# --------------------------------------------------------------------------- #
# Rate limiting for the auth endpoints (§55)
# --------------------------------------------------------------------------- #


def _prune_stale_auth_buckets(now: float) -> None:
    """Drop expired rate-limit buckets so the dict cannot grow unbounded.

    Buckets are keyed by ``ip:username``; without pruning an attacker (or a
    /24 of clients) grows the dict without limit. Called opportunistically
    once the map passes _AUTH_PRUNE_THRESHOLD keys — O(n) walk, rare.
    """
    if len(_auth_attempts) < _AUTH_PRUNE_THRESHOLD:
        return
    stale = [
        key
        for key, window in _auth_attempts.items()
        if not window or now - window[-1] > _AUTH_WINDOW_SECONDS
    ]
    for key in stale:
        del _auth_attempts[key]


def _shared_rate_limit(client_key: str) -> None:
    """Count one attempt for ``client_key`` in the shared store and 429 on
    the 6th within the window (§55 / TD-015)."""

    deadline = time.monotonic() + _SHARED_TIMEOUT_SECONDS
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("rate_limit.shared.timeout")
            return
        try:
            with _shared_session_scope() as session:
                session.execute(
                    text(f"SET LOCAL lock_timeout = '{_SHARED_LOCK_TIMEOUT_MS}'")
                )
                session.execute(
                    text(
                        f"SET LOCAL statement_timeout = "
                        f"'{_SHARED_STATEMENT_TIMEOUT_MS}'"
                    )
                )
                _store_lock_bucket(session, client_key)
                now = datetime.now(tz=UTC)
                window_start = now - timedelta(seconds=_AUTH_WINDOW_SECONDS)
                seen = _store_count_recent(session, client_key, window_start)
                if seen >= _AUTH_MAX_ATTEMPTS:
                    raise HTTPException(
                        status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts"
                    )
                _store_record(session, client_key, now)
                if random.random() < _SHARED_PRUNE_PROBABILITY and _store_prune(
                    session, before=window_start
                ):
                    logger.debug("rate_limit.shared.pruned")
                session.commit()
            return
        except HTTPException:
            raise
        except Exception as exc:
            if _store_is_persistent_error(exc):
                logger.warning(
                    "rate_limit.shared.unavailable",
                    extra={"err": type(exc).__name__},
                )
                return
            time.sleep(min(0.05 * (attempt + 1), max(0.0, remaining)))
    logger.warning("rate_limit.shared.exhausted_retries")


def auth_rate_limit(client_key: str) -> None:
    """Reject the 6th attempt within a minute for the same client (§55).

    TD-015: when RATE_LIMIT_SHARED=1 the sliding window lives in the
    ``auth_rate_events`` table, so the limit holds across workers and
    replicas. The in-process window is then only a per-worker fast path
    that stops an attacker from hitting the database 5 times for free.

    Failure policy is fail-open (ADR-014): if the shared store is
    unavailable or too slow, the per-worker window still enforces the
    limit locally, so a database outage must not take the login endpoint
    (and therefore the whole API surface) down. The local window keeps
    counting during the outage, and the shared store catches up with the
    missed events once it recovers.
    """
    if _RATE_LIMIT_SHARED:
        _shared_rate_limit(client_key)
    now = time.monotonic()
    _prune_stale_auth_buckets(now)
    window = _auth_attempts[client_key]
    while window and now - window[0] > _AUTH_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= _AUTH_MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts")
    window.append(now)


def reset_auth_rate_limit() -> None:
    """Test helper: forget all recorded auth attempts."""
    _auth_attempts.clear()
    if _RATE_LIMIT_SHARED:
        from app.core.database import session_scope

        try:
            with session_scope() as session:
                deleted = _store_clear_all(session)
            if deleted:
                logger.info("rate_limit.shared.cleared", extra={"rows": deleted})
        except Exception:
            logger.warning("rate_limit.shared.reset_unavailable")
