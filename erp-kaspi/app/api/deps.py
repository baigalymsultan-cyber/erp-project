"""Shared API dependencies — DB session, auth helpers, utilities.

Imported by every ``app.api.<domain>`` router module. No route definitions
here; this module must not import from ``app.api`` (no cycles).

Transaction policy (§9):
    * API routes run inside the request transaction opened by ``get_db``
      (``session.begin()``). Routes MUST NOT call ``session.commit()`` —
      the context manager commits atomically on success, rolls back on
      error. Use ``session.flush()`` when you need generated IDs mid-request.
    * Services MUST NOT call ``session.commit()``. They execute within the
      caller's transaction (request or explicit).
    * Exceptions (documented at the call site):
      - bulk ETL (bank_import.StatementImportService) manages its own
        transactions internally (chunked commits P0-2, fail_batch,
        dry_run); it is idempotent via file hash, so partial work is
        resumable. It does not participate in the request transaction.
      - auth audit (failed logins, _db_session) commits independently —
        the audit trail must persist even if the request fails.
"""

from __future__ import annotations

import decimal
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_session_factory, set_current_user
from app.core.policy import ensure_ip_access_or_all
from app.core.security import decode_token
from app.models.hr import Employee

_bearer = HTTPBearer(auto_error=False)


logger = logging.getLogger("erp.api")


logger.setLevel(logging.INFO)


def get_db(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Iterator[Session]:
    """FastAPI dependency — one session per request, commit or rollback (§16).

    Plain generator function: FastAPI unwraps yield-deps itself, so it must
    NOT be wrapped with @contextmanager (TD-011 lesson).

    §54 — the audit trigger fn_audit() reads app.current_user_id. The bearer
    token (when present) is decoded here so every authenticated request
    writes audit rows with the acting user instead of NULL. Endpoints without
    authentication (/health, /auth/login) keep user_id NULL, which
    fn_current_app_user() turns into NULL rather than a faked value.
    """
    factory = get_session_factory()
    with factory() as session, session.begin():
        if credentials is not None:
            try:
                claims = decode_token(credentials.credentials)
                sub = claims.get("sub")
                if isinstance(sub, (int, str)) and str(sub).isdigit():
                    set_current_user(session, int(sub))
            except HTTPException:
                pass  # token problems are 401'd by current_principal later
        yield session


@contextmanager
def _db_session() -> Iterator[Session]:
    """Context-manager form of get_db for services and tests."""
    factory = get_session_factory()
    with factory() as session, session.begin():
        yield session


def _decimal_safe(row: dict[str, object]) -> dict[str, object]:
    """JSON-safe row: Decimal → str, date/datetime → ISO (REQ-TD016-05)."""
    out: dict[str, object] = {}
    for key, value in row.items():
        if isinstance(value, decimal.Decimal):
            out[key] = str(value)
        elif isinstance(value, (datetime, date)):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


class _FiveHundredGauge:
    def __init__(self) -> None:
        self.count = 0


APP_5XX = _FiveHundredGauge()


def ensure_employee_access(
    session: Session,
    principal: dict[str, object],
    employee_id: int,
) -> None:
    """Object-level check for employee-scoped resources (BOLA, audit #1).

    The employee's ИП assignment must be inside the principal's allow-list;
    ADMIN/DIRECTOR pass unrestricted.
    """

    allowed = ensure_ip_access_or_all(session, principal)
    if allowed is None:
        return
    assigned = session.scalar(select(Employee.assigned_ip_id).where(Employee.id == employee_id))
    if assigned is None or assigned not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to this ИП")


def _client_ip(request: Request) -> str:
    """The real client IP, honouring a trusted reverse proxy (audit #8).

    ``TRUST_PROXY=true`` (the documented production deployment is
    Caddy/nginx → uvicorn) takes the left-most X-Forwarded-For entry set by
    the local proxy; otherwise the socket peer is used and the header is
    ignored, so a client cannot spoof a fresh bucket.
    """
    import os

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and os.environ.get("TRUST_PROXY", "").lower() in {"1", "true", "yes"}:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _stock_created_by(principal: dict[str, object]) -> int | None:
    user_id = principal.get("user_id")
    return int(user_id) if isinstance(user_id, int) else None


def _stock_error(exc: ValueError) -> HTTPException:
    """ValueError → 404 for missing rows, 422 for validation failures."""
    if "not found" in str(exc):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
