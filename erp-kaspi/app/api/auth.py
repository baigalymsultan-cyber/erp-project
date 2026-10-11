"""Authentication + user management routes (MASTER PROMPT §52/§55)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from app.api.deps import _client_ip, _db_session, get_db
from app.core.auth import AuditLog, Role, User, UserRole
from app.core.policy import allowed_ip_ids
from app.core.security import (
    LOCKOUT_MINUTES,
    LOCKOUT_THRESHOLD,
    Principal,
    auth_rate_limit,
    current_principal,
    hash_password,
    issue_token,
    require_privilege,
    user_roles,
    verify_password,
)
from app.schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetRequest,
    TokenResponse,
    UserCreate,
)

router = APIRouter()


_LOCKOUT_THRESHOLD = LOCKOUT_THRESHOLD
_LOCKOUT_MINUTES = LOCKOUT_MINUTES


def _record_failed_login(user_id: int, now: datetime) -> None:
    """§72 — persist users.failed_logins in its own short transaction.

    The request-scoped transaction (get_db, §16) is rolled back when the 401
    propagates, so an ORM attribute change on the request session would be
    silently lost. The counter is therefore incremented with a standalone
    UPDATE that commits before the HTTPException is raised.
    """
    with _db_session() as s:
        # locked_until is set when the threshold is reached; a successful
        # login below clears both fields (audit #9 — full lockout cycle).
        s.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                failed_logins=User.failed_logins + 1,
                locked_until=case(
                    (
                        User.failed_logins + 1 >= _LOCKOUT_THRESHOLD,
                        now + timedelta(minutes=_LOCKOUT_MINUTES),
                    ),
                    else_=None,
                ),
            )
        )
        # §54 — the failed attempt itself is an audit event of the auth
        # domain, committed in the same standalone transaction (the request
        # transaction rolls back with the 401).
        s.add(
            AuditLog(
                user_id=user_id,
                entity="auth",
                action="LOGIN_FAILED",
                new_data={"reason": "invalid"},
            )
        )


def _audit_auth(user_id: int, action: str, data: dict[str, str]) -> None:
    """§54 — an auth audit row in its own transaction.

    Used on paths that raise HTTPException (locked / inactive logins): the
    request-scoped transaction would roll the row back with the error.
    """
    with _db_session() as s:
        s.add(AuditLog(user_id=user_id, entity="auth", action=action, new_data=data))


@router.post("/auth/login", response_model=TokenResponse, tags=["auth"])
def login(
    data: LoginRequest,
    request: Request,
    session: Session = Depends(get_db),
) -> TokenResponse:
    """Authenticate with username/password, return a JWT (§55).

    Rate limited to 5 attempts / 60 s per client IP + username bucket
    (audit #8 — one global "unknown" bucket used to lock out everyone).
    Accounts are temporarily locked after repeated failures (audit #9:
    users.locked_until existed but was never enforced).
    """
    auth_rate_limit(f"{_client_ip(request)}:{data.username}")

    stmt = select(User).where(User.username == data.username)
    user = session.execute(stmt).scalar_one_or_none()

    # Uniform 401 for unknown user and bad password — no user enumeration.
    # §72 — the failure is recorded on the account (users.failed_logins) so
    # /monitoring/summary can report authentication failures.
    # §55/audit #9 — an active lockout refuses the password check entirely.
    now_utc = datetime.now(UTC)
    if user is not None and user.locked_until is not None and user.locked_until > now_utc:
        _audit_auth(user.id, "LOGIN_FAILED", {"reason": "locked"})
        raise HTTPException(status.HTTP_423_LOCKED, "account temporarily locked")

    if user is None or not verify_password(data.password, user.password_hash):
        if user is not None:
            _record_failed_login(user.id, now_utc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not user.is_active:
        _audit_auth(user.id, "LOGIN_FAILED", {"reason": "inactive"})
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account disabled")

    # get_db owns the transaction (§16); an explicit commit here would close
    # the surrounding context and break subsequent statements.
    user.last_login_at = now_utc
    user.failed_logins = 0
    user.locked_until = None

    roles = user_roles(session, user.id)
    token = issue_token(user, roles)
    # §54 — successful logins are audited with the acting user id in the
    # same request transaction (get_db commits it on the 200 response).
    session.add(AuditLog(user_id=user.id, entity="auth", action="LOGIN", new_data={"via": "api"}))
    return TokenResponse(access_token=token, expires_in=8 * 60 * 60, roles=roles)


@router.post("/auth/password-change", tags=["auth"])
def change_password(
    data: PasswordChangeRequest,
    request: Request,
    principal: Annotated[dict[str, object], Depends(current_principal)],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Self-service password change (MASTER PROMPT §17).

    The current password must be re-presented; on success every JWT issued
    before ``now`` is revoked via ``users.password_changed_at``. The row
    UPDATE is audit-logged by fn_audit() with the acting user id.
    """
    # Brute-force guard on current_password — same bucket family as login.
    auth_rate_limit(f"pwchange:{_client_ip(request)}:{principal['user_id']}")

    user = session.get(User, int(cast("int", principal["user_id"])))
    if user is None or not verify_password(data.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")
    user.password_hash = hash_password(data.new_password)
    user.password_changed_at = datetime.now(UTC)
    user.token_version = int(user.token_version or 1) + 1
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return {"status": "changed", "tokens_revoked_before": user.password_changed_at.isoformat()}


@router.post("/auth/users/{user_id}/reset-password", tags=["auth"])
def reset_password(
    user_id: int,
    data: PasswordResetRequest,
    principal: Annotated[dict[str, object], Depends(require_privilege("ADMIN"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """ADMIN-mediated password reset (MASTER PROMPT §17 — reset flow).

    No email infrastructure is specified (OQ-005), so the reset is
    admin-issued: the admin sets a temporary password; the target user is
    expected to change it via /auth/password-change. All JWTs issued before
    the reset are revoked for the target account.
    """
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    user.password_hash = hash_password(data.new_password)
    user.password_changed_at = datetime.now(UTC)
    user.token_version = int(user.token_version or 1) + 1
    user.failed_logins = 0
    user.locked_until = None
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only
    return {"status": "reset", "user_id": user_id}


@router.post("/auth/users", status_code=status.HTTP_201_CREATED, tags=["auth"])
def create_user(
    data: UserCreate,
    principal: Annotated[dict[str, object], Depends(require_privilege("ADMIN"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Create a user with roles (ADMIN only, §53)."""
    existing = session.execute(
        select(User).where(User.username == data.username)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already exists")

    from app.core.security import hash_password

    unknown = set(data.roles) - {r.code for r in session.execute(select(Role)).scalars()}
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown roles: {sorted(unknown)}"
        )

    user = User(
        username=data.username,
        email=data.email,
        full_name=data.full_name,
        password_hash=hash_password(data.password),
        is_active=True,
    )
    session.add(user)
    session.flush()

    for code in data.roles:
        role = session.execute(select(Role).where(Role.code == code)).scalar_one()
        session.add(UserRole(user_id=user.id, role_id=role.id))
    session.flush()  # §9: request transaction commits in get_db; flush for IDs only

    return {
        "user_id": user.id,
        "username": user.username,
        "roles": data.roles,
    }


@router.get("/auth/users", tags=["auth"])
def list_users(
    principal: Annotated[dict[str, object], Depends(require_privilege("ADMIN"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """TD-016 E17 — user/role matrix for the UI admin module (ADMIN+)."""
    rows = session.execute(
        select(User, Role.code)
        .join(UserRole, UserRole.user_id == User.id, isouter=True)
        .join(Role, Role.id == UserRole.role_id, isouter=True)
        .order_by(User.id)
    )
    users: dict[int, dict[str, object]] = {}
    for user, role_code in rows:
        entry = users.get(user.id)
        if entry is None:
            entry = {
                "id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "is_active": user.is_active,
                "roles": [],
            }
            users[user.id] = entry
        if role_code is not None:
            roles = cast("list[str]", entry["roles"])
            roles.append(role_code)
    return {"total": len(users), "items": list(users.values())}


@router.get("/auth/me", tags=["auth"])
def me(principal: Principal, session: Session = Depends(get_db)) -> dict[str, object]:
    """Return the authenticated principal (§53) with the ИП allow-list.

    TD-016: the UI replaces its direct user_ip_access SQL with this field
    when populating the ИП filter pickers (REQ-TD016-04 context).
    """
    return {
        **principal,
        "allowed_ip_ids": allowed_ip_ids(
            session,
            cast(int, principal["user_id"]),
            cast(list[str], principal["roles"]),
        ),
    }


@router.post("/auth/logout", tags=["auth"])
def logout(
    principal: Annotated[dict[str, object], Depends(current_principal)],
) -> dict[str, object]:
    """Server-side logout — §17 session invalidation (v8 Phase A).

    A stateless JWT cannot be individually recalled without a per-token
    denylist (rejected as overkill for this deployment — see ADR-013). The
    supported semantic is *logout everywhere*: the caller's token version
    is bumped, so every token minted before this instant — on any device —
    stops working at the next request. There is no client-side-only discard.
    The users UPDATE is audit-logged by fn_audit().
    """
    with _db_session() as s:
        uid = cast(int, principal["user_id"])
        user = s.execute(select(User).where(User.id == uid)).scalar_one()
        user.token_version = int(user.token_version or 1) + 1
        # §54 — the LOGOUT event (server-side revocation) is audited with
        # the acting user id in the same committing transaction.
        s.add(AuditLog(user_id=uid, entity="auth", action="LOGOUT", new_data={"via": "api"}))
        s.commit()
    return {"revoked_before": datetime.now(UTC).isoformat(), "scope": "all-sessions"}


@router.post("/auth/users/{user_id}/revoke-sessions", tags=["auth"])
def revoke_sessions(
    user_id: int,
    principal: Annotated[dict[str, object], Depends(require_privilege("ADMIN"))],
) -> dict[str, object]:
    """ADMIN emergency revocation — §17 (v8 Phase A).

    Bumps the target user's token_version: every outstanding JWT for that
    account — including stolen ones — is rejected from this moment. Other
    accounts are untouched (one row-locked UPDATE, no global effect).
    """
    with _db_session() as s:
        user = s.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
        user.token_version = int(user.token_version or 1) + 1
        admin_id = cast(int, principal["user_id"])
        s.add(
            AuditLog(
                user_id=admin_id,
                entity="users",
                entity_id=user_id,
                action="REVOKE_SESSIONS",
                new_data={"token_version": user.token_version},
            )
        )
        s.commit()
    return {
        "user_id": user_id,
        "revoked_before": datetime.now(UTC).isoformat(),
        "scope": "all-sessions",
    }
