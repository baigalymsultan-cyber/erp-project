"""
Multi-IP (multi-ИП) authorization policy — OWASP API1 (BOLA/IDOR).

A role alone is not authorization: every request that touches an object must
also verify that the object belongs to an ИП the principal may see.

Access model (least privilege, no new tables required):
  * ADMIN, DIRECTOR — all ИП (the owners and their administrators);
  * every other role — only the ИП assigned to the employee linked to the
    user (users.employee_id → employees.assigned_ip_id);
  * a user with no employee link (or an employee without an assignment)
    gets NO ИП access — an empty allow-list denies everything except the
    ADMIN/DIRECTOR escape hatch.

The checks are centralised here so the API layer cannot forget one:
``require_ip_access`` / ``ensure_ip_access`` are the only sanctioned way to
answer "may this principal act on this ИП?".
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import User
from app.models.hr import Employee
from app.models.reference import IpEntity

#: Roles that see every ИП without an explicit assignment.
GLOBAL_IP_ROLES = frozenset({"ADMIN", "DIRECTOR"})

#: Cache TTL for the id→role→ИП resolution inside one request is unnecessary:
#: the helpers take the request-scoped session, so the cache lifetime is the
#: transaction — no cross-request staleness is possible.


def allowed_ip_ids(session: Session, user_id: int, roles: list[str]) -> list[int] | None:
    """The ИП ids the user may act on. ``None`` means "all ИП".

    ADMIN/DIRECTOR → None (unrestricted). Everyone else → the ИП assigned to
    their linked employee, possibly an empty list.
    """
    if GLOBAL_IP_ROLES & set(roles):
        return None
    assigned = session.scalar(
        select(Employee.assigned_ip_id)
        .join(User, User.employee_id == Employee.id)
        .where(User.id == user_id)
    )
    return [assigned] if assigned is not None else []


def ip_access_error() -> dict[str, object]:
    """Uniform 403 body — do not reveal whether the object exists."""
    return {"detail": "no access to this ИП"}


def ensure_ip_access(
    session: Session,
    principal: dict[str, object],
    ip_id: int | None,
) -> None:
    """Raise 403 unless the principal may act on `ip_id`.

    `ip_id=None` is allowed only for unrestricted principals (ADMIN/DIRECTOR);
    restricted principals must name an ИП explicitly — guessing is denied.
    """
    from fastapi import HTTPException, status

    roles = cast("list[str]", principal.get("roles", []))
    user_id = principal.get("user_id")
    allowed = allowed_ip_ids(session, int(user_id) if isinstance(user_id, int) else 0, roles)
    if allowed is None:
        return
    if ip_id is None or ip_id not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to this ИП")


def ensure_ip_access_or_all(session: Session, principal: dict[str, object]) -> list[int] | None:
    """Return the allow-list for list endpoints (``None`` = no filtering)."""
    roles = cast("list[str]", principal.get("roles", []))
    user_id = principal.get("user_id")
    return allowed_ip_ids(session, int(user_id) if isinstance(user_id, int) else 0, roles)


def assert_ip_exists(session: Session, ip_id: int) -> None:
    """404 when the ИП does not exist at all (distinct from 403 no-access)."""
    from fastapi import HTTPException, status

    if session.get(IpEntity, ip_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"ip {ip_id} not found")


__all__ = [
    "GLOBAL_IP_ROLES",
    "allowed_ip_ids",
    "assert_ip_exists",
    "ensure_ip_access",
    "ensure_ip_access_or_all",
    "ip_access_error",
]
