"""Inter-IP transfer routes — §28."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.policy import assert_ip_exists, ensure_ip_access
from app.core.security import require_privilege
from app.schemas.finance import TransferCreate
from app.services import TransferIdempotencyConflict, add_transfer

router = APIRouter()


@router.post("/transfers", status_code=status.HTTP_201_CREATED, tags=["transfers"])
def post_transfer(
    data: TransferCreate,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
    principal: Annotated[dict[str, object], Depends(require_privilege("ACCOUNTANT"))],
    session: Session = Depends(get_db),
) -> dict[str, object]:
    ensure_ip_access(session, principal, data.from_ip_id)
    ensure_ip_access(session, principal, data.to_ip_id)
    assert_ip_exists(session, data.from_ip_id)
    assert_ip_exists(session, data.to_ip_id)
    actor_id = principal.get("user_id")
    if not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id <= 0:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authenticated user is required")
    try:
        transfer = add_transfer(
            session,
            data,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
        )
    except TransferIdempotencyConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {
        "id": transfer.id,
        "status": "POSTED",
        "excluded_from_pnl": True,
        "outbound_operation_id": transfer.outbound_operation_id,
        "inbound_operation_id": transfer.inbound_operation_id,
    }
