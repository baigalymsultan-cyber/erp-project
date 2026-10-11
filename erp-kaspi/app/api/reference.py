"""Reference data routes — IPs, products, materials, accounts."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.policy import ensure_ip_access, ensure_ip_access_or_all
from app.core.security import require_privilege
from app.models.hr import WorkOperationType
from app.models.reference import Account, IpEntity, Material, Product

router = APIRouter()


@router.get("/ips", tags=["reference"])
def list_ips(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """TD-016 E22 — ИП pickers for the UI (replaces A6/A9/C1 ip SQL).

    Restricted principals get only their allow-list (REQ-TD016-02); an
    unrestricted one gets the full registry. Names only — no financial data.
    """
    allowed = ensure_ip_access_or_all(session, principal)
    stmt = select(IpEntity.id, IpEntity.name).order_by(IpEntity.name)
    if allowed is not None:
        stmt = stmt.where(IpEntity.id.in_(allowed))
    return {"items": [{"id": r.id, "name": r.name} for r in session.execute(stmt)]}


@router.get("/products/summary", tags=["reference"])
def products_summary(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    q: str = Query(default="", max_length=200),
    active: bool | None = Query(default=None),
) -> dict[str, object]:
    """TD-016 E20 — KPI for UI 'Товары' (replaces A16 KPI SQL)."""
    del principal  # reference data, no ИП scope (same as E9)
    like = f"%{(q or '').strip()}%"
    stmt = select(Product.selling_price)
    if like != "%%":
        stmt = stmt.where(or_(Product.sku.ilike(like), Product.name.ilike(like)))
    if active is not None:
        stmt = stmt.where(Product.is_active.is_(active))
    prices = session.execute(stmt).scalars().all()
    avg = sum(prices, Decimal("0")) / len(prices) if prices else Decimal("0")
    return {"count": len(prices), "avg_price": str(avg)}


@router.get("/products", tags=["reference"])
def list_products(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    is_active: bool | None = Query(default=None),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E9 — product catalogue (§46); global, read-only."""
    like = f"%{q.strip()}%"
    stmt = select(Product).where(Product.name.ilike(like))
    if is_active is not None:
        stmt = stmt.where(Product.is_active.is_(is_active))
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(stmt.order_by(Product.sku).limit(limit).offset(offset)).scalars()
    return {
        "total": int(total),
        "items": [
            {
                "id": r.id,
                "sku": r.sku,
                "name": r.name,
                "unit": r.unit,
                "selling_price": str(r.selling_price),
                "is_active": r.is_active,
            }
            for r in rows
        ],
    }


@router.get("/materials", tags=["reference"])
def list_materials(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    is_active: bool | None = Query(default=None),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """TD-016 E10 — materials catalogue (§48); global, read-only."""
    like = f"%{q.strip()}%"
    stmt = select(Material).where(Material.name.ilike(like))
    if is_active is not None:
        stmt = stmt.where(Material.is_active.is_(is_active))
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(stmt.order_by(Material.sku).limit(limit).offset(offset)).scalars()
    return {
        "total": int(total),
        "items": [
            {
                "id": r.id,
                "sku": r.sku,
                "name": r.name,
                "unit": r.unit,
                "current_price": str(r.current_price),
                "is_active": r.is_active,
            }
            for r in rows
        ],
    }


@router.get("/accounts", tags=["reference"])
def list_accounts(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
    is_active: bool | None = Query(default=None),
    ip_id: int | None = Query(default=None),
) -> dict[str, object]:
    """TD-016 E11 — bank/cash accounts, ИП-isolated (UI 'Счета ИП')."""
    allowed = ensure_ip_access_or_all(session, principal)
    stmt = select(Account)
    if ip_id is not None:
        ensure_ip_access(session, principal, ip_id)
        stmt = stmt.where(Account.ip_id == ip_id)
    elif allowed is not None:
        stmt = stmt.where(Account.ip_id.in_(allowed))
    if is_active is not None:
        stmt = stmt.where(Account.is_active.is_(is_active))
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(stmt.order_by(Account.ip_id, Account.name)).scalars()
    return {
        "total": int(total),
        "items": [
            {
                "id": r.id,
                "ip_id": r.ip_id,
                "name": r.name,
                "type": r.type,
                "is_active": r.is_active,
            }
            for r in rows
        ],
    }


@router.get("/work-operation-types", tags=["reference"])
def list_work_operation_types(
    principal: Annotated[dict[str, object], Depends(require_privilege("VIEWER"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """TD-016 E15 — active piecework operation types (UI work-entry form)."""
    rows = session.execute(
        select(
            WorkOperationType.id,
            WorkOperationType.code,
            WorkOperationType.name,
            WorkOperationType.unit,
            WorkOperationType.calc_mode,
        )
        .where(WorkOperationType.is_active.is_(True))
        .order_by(WorkOperationType.name)
    )
    return {
        "items": [
            {
                "id": r.id,
                "code": r.code,
                "name": r.name,
                "unit": r.unit,
                "calc_mode": r.calc_mode,
            }
            for r in rows
        ]
    }
