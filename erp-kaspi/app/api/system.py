"""System routes — health, monitoring, audit log, ETL batches."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import APP_5XX, get_db
from app.core.auth import AuditLog, User
from app.core.policy import ensure_ip_access_or_all
from app.core.security import require_privilege
from app.models.finance import ImportBatch
from app.models.kaspi import SyncLog

router = APIRouter()


@router.get("/health", tags=["system"])
def health(session: Session = Depends(get_db)) -> dict[str, object]:
    tables = {
        "ip_entities": True,
        "operations": True,
        "orders": True,
        "transfers": True,
        "payroll_periods": True,
        "kaspi_payouts": True,
    }
    missing = [
        name
        for name in tables
        if session.execute(select(func.to_regclass(f"public.{name}"))).scalar_one() is None
    ]
    return {
        "status": "ok" if not missing else "degraded",
        "missing_tables": missing,
        "schema": "STEP 1 applied (§90)",
    }


@router.get("/monitoring/summary", tags=["system"])
def monitoring_summary(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Operational snapshot per §72.

    Covers: application errors (process-local 5xx gauge), database
    reachability + schema presence, Kaspi API errors and failed syncs
    (sync_log, last 24h), stale sync jobs (RUNNING > 1h) and
    authentication failures (users.failed_logins).
    """
    del principal

    tables = (
        "ip_entities",
        "operations",
        "orders",
        "transfers",
        "payroll_periods",
        "kaspi_payouts",
    )
    missing = [
        name
        for name in tables
        if session.execute(select(func.to_regclass(f"public.{name}"))).scalar_one() is None
    ]

    sync_rows = session.execute(
        select(
            func.count()
            .filter(
                SyncLog.status == "FAILED",
                SyncLog.sync_started_at >= func.now() - func.make_interval(0, 0, 0, 1),
            )
            .label("failed"),
            func.count()
            .filter(
                SyncLog.status == "PARTIAL",
                SyncLog.sync_started_at >= func.now() - func.make_interval(0, 0, 0, 1),
            )
            .label("partial"),
            func.coalesce(
                func.sum(SyncLog.errors_count).filter(
                    SyncLog.sync_started_at >= func.now() - func.make_interval(0, 0, 0, 1)
                ),
                0,
            ).label("errors_total"),
            func.count()
            .filter(
                SyncLog.status == "RUNNING",
                SyncLog.sync_started_at < func.now() - func.make_interval(0, 0, 0, 0, 1),
            )
            .label("stale_running"),
        ).select_from(SyncLog)
    ).one()
    failed, partial, errors_total, stale_running = (
        int(sync_rows.failed),
        int(sync_rows.partial),
        int(sync_rows.errors_total),
        int(sync_rows.stale_running),
    )

    auth_row = session.execute(
        select(
            func.coalesce(func.sum(User.failed_logins), 0).label("failed_logins_total"),
            func.count().filter(User.failed_logins > 0).label("users_with_failures"),
        ).select_from(User)
    ).one()

    return {
        "application": {
            "errors_5xx_since_start": APP_5XX.count,
            "note": "process-local gauge; resets on restart",
        },
        "database": {
            "ok": not missing,
            "missing_tables": missing,
        },
        "kaspi_sync_last_24h": {
            "failed": failed,
            "partial": partial,
            "errors_total": errors_total,
            "stale_running": stale_running,
        },
        "auth": {
            "failed_logins_total": int(auth_row.failed_logins_total),
            "users_with_failures": int(auth_row.users_with_failures),
        },
    }


@router.get("/import-batches", tags=["etl"])
def list_import_batches(
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E13 — ETL batch registry (UI 'Импорт Excel', §39–§41)."""
    # P1-3: scope to the principal's IP allow-list. ImportBatch.ip_id is
    # nullable (legacy batches); NULL batches stay visible to restricted users
    # (explicit choice — they carry no IP attribution).
    allowed = ensure_ip_access_or_all(session, principal)
    base = select(ImportBatch)
    if allowed is not None:
        base = base.where(or_(ImportBatch.ip_id.in_(allowed), ImportBatch.ip_id.is_(None)))
    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    running = session.execute(
        select(func.count()).select_from(base.where(ImportBatch.status == "RUNNING").subquery())
    ).scalar_one()
    rows = session.execute(
        base.order_by(ImportBatch.id.desc()).limit(limit).offset(offset)
    ).scalars()
    return {
        "total": int(total),
        "kpi": {"running": int(running), "total": int(total)},
        "items": [
            {
                "id": r.id,
                "source_type": r.source_type,
                "source_name": r.source_name,
                "status": r.status,
                "rows_total": r.rows_total,
                "rows_inserted": r.rows_inserted,
                "rows_skipped": r.rows_skipped,
                "started_at": r.started_at.isoformat() if r.started_at else None,
            }
            for r in rows
        ],
    }


@router.get("/audit-log", tags=["audit"])
def list_audit_log(
    principal: Annotated[dict[str, object], Depends(require_privilege("DIRECTOR"))],
    session: Session = Depends(get_db),
    entity: str | None = Query(default=None, max_length=100),
    entity_id: int | None = Query(default=None),
    user_id: int | None = Query(default=None),
    action: str | None = Query(default=None, max_length=100),
    created_from: date | None = Query(default=None),
    created_to: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E16 — append-only audit trail (UI 'Аудит', DIRECTOR+)."""
    stmt = select(
        AuditLog.id,
        AuditLog.user_id,
        AuditLog.entity,
        AuditLog.entity_id,
        AuditLog.action,
        AuditLog.new_data,
        AuditLog.created_at,
        User.username.label("username"),
    ).outerjoin(User, User.id == AuditLog.user_id)
    count_stmt = select(func.count()).select_from(AuditLog)
    if entity is not None:
        stmt = stmt.where(AuditLog.entity == entity)
        count_stmt = count_stmt.where(AuditLog.entity == entity)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
        count_stmt = count_stmt.where(AuditLog.entity_id == entity_id)
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
        count_stmt = count_stmt.where(AuditLog.user_id == user_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    if created_from is not None:
        stmt = stmt.where(AuditLog.created_at >= created_from)
        count_stmt = count_stmt.where(AuditLog.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(AuditLog.created_at < created_to + timedelta(days=1))
        count_stmt = count_stmt.where(AuditLog.created_at < created_to + timedelta(days=1))
    total = session.execute(count_stmt).scalar_one()
    rows = session.execute(stmt.order_by(AuditLog.id.desc()).limit(limit).offset(offset))
    return {
        "total": int(total),
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": r.username,
                "entity": r.entity,
                "entity_id": r.entity_id,
                "action": r.action,
                "new_data": r.new_data,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }
