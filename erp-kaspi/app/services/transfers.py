"""Inter-IP transfer services — §28."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.database import set_current_user
from app.models.finance import Operation, Transfer
from app.models.reference import Account, OperationType
from app.schemas import TransferCreate


class TransferIdempotencyConflict(ValueError):
    """The same request key was already committed for a different payload."""


@dataclass(frozen=True)
class TransferResult:
    id: int
    outbound_operation_id: int
    inbound_operation_id: int


class _ConcurrentTransferReplay(Exception):
    pass


def _transfer_result(transfer: Transfer) -> TransferResult:
    if (
        transfer.status != "POSTED"
        or transfer.outbound_operation_id is None
        or transfer.inbound_operation_id is None
    ):
        raise RuntimeError("idempotent transfer record is incomplete or not posted")
    return TransferResult(
        id=transfer.id,
        outbound_operation_id=transfer.outbound_operation_id,
        inbound_operation_id=transfer.inbound_operation_id,
    )


def _same_transfer_payload(transfer: Transfer, data: TransferCreate) -> bool:
    return (
        transfer.from_ip_id == data.from_ip_id
        and transfer.to_ip_id == data.to_ip_id
        and transfer.from_account_id == data.from_account_id
        and transfer.to_account_id == data.to_account_id
        and Decimal(transfer.amount) == Decimal(data.amount)
        and transfer.transfer_date == data.transfer_date
        and transfer.reason == data.comment
    )


def add_transfer(
    session: Session,
    data: TransferCreate,
    *,
    idempotency_key: str,
    actor_id: int,
) -> TransferResult:
    """Create the cross-IP transfer aggregate and both ledger legs atomically."""
    key = idempotency_key.strip()
    if not key or len(key) > 255:
        raise ValueError("Idempotency-Key must contain 1 to 255 non-whitespace characters")
    if data.from_ip_id == data.to_ip_id:
        raise ValueError("transfer must be between different IP entities")
    if data.from_account_id == data.to_account_id:
        raise ValueError("source and destination accounts must differ")
    if actor_id <= 0:
        raise ValueError("transfer actor must be an authenticated user")

    set_current_user(session, actor_id)
    existing = session.scalar(select(Transfer).where(Transfer.idempotency_key == key))
    if existing is not None:
        if not _same_transfer_payload(existing, data):
            raise TransferIdempotencyConflict(
                "Idempotency-Key was already used for a different transfer payload"
            )
        return _transfer_result(existing)

    for account_id, ip_id, label in (
        (data.from_account_id, data.from_ip_id, "source"),
        (data.to_account_id, data.to_ip_id, "destination"),
    ):
        account = session.get(Account, account_id)
        if account is None or account.ip_id != ip_id or not account.is_active:
            raise ValueError(f"{label} account must be active and belong to its transfer IP")

    operation_types = {
        item.code: item
        for item in session.scalars(
            select(OperationType).where(OperationType.code.in_(("TRANSFER_OUT", "TRANSFER_IN")))
        )
    }
    outbound_type = operation_types.get("TRANSFER_OUT")
    inbound_type = operation_types.get("TRANSFER_IN")
    if (
        outbound_type is None
        or inbound_type is None
        or outbound_type.direction != "OUT"
        or inbound_type.direction != "IN"
        or outbound_type.kind != "TRANSFER"
        or inbound_type.kind != "TRANSFER"
        or outbound_type.affects_pnl
        or inbound_type.affects_pnl
    ):
        raise RuntimeError("TRANSFER_OUT/TRANSFER_IN operation types are missing or misconfigured")

    try:
        with session.begin_nested():
            outbound = Operation(
                ip_id=data.from_ip_id,
                type_id=outbound_type.id,
                account_id=data.from_account_id,
                amount=-Decimal(data.amount),
                operation_date=data.transfer_date,
                comment=data.comment,
                status="DRAFT",
                created_by=actor_id,
            )
            inbound = Operation(
                ip_id=data.to_ip_id,
                type_id=inbound_type.id,
                account_id=data.to_account_id,
                amount=Decimal(data.amount),
                operation_date=data.transfer_date,
                comment=data.comment,
                status="DRAFT",
                created_by=actor_id,
            )
            session.add_all((outbound, inbound))
            session.flush()
            outbound.related_operation_id = inbound.id
            inbound.related_operation_id = outbound.id
            session.flush()

            transfer_id = session.scalar(
                pg_insert(Transfer)
                .values(
                    from_ip_id=data.from_ip_id,
                    to_ip_id=data.to_ip_id,
                    from_account_id=data.from_account_id,
                    to_account_id=data.to_account_id,
                    amount=Decimal(data.amount),
                    transfer_date=data.transfer_date,
                    reason=data.comment,
                    outbound_operation_id=outbound.id,
                    inbound_operation_id=inbound.id,
                    idempotency_key=key,
                    status="DRAFT",
                    created_by=actor_id,
                )
                .on_conflict_do_nothing(index_elements=[Transfer.idempotency_key])
                .returning(Transfer.id)
            )
            if transfer_id is None:
                raise _ConcurrentTransferReplay

            transfer = session.get(Transfer, int(transfer_id))
            if transfer is None:
                raise RuntimeError("new transfer disappeared inside its transaction")
            outbound.status = "POSTED"
            inbound.status = "POSTED"
            transfer.status = "POSTED"
            session.flush()
            result = _transfer_result(transfer)
        return result
    except _ConcurrentTransferReplay as exc:
        existing = session.scalar(select(Transfer).where(Transfer.idempotency_key == key))
        if existing is None:
            raise RuntimeError("transfer idempotency conflict has no committed result") from exc
        if not _same_transfer_payload(existing, data):
            raise TransferIdempotencyConflict(
                "Idempotency-Key was already used for a different transfer payload"
            ) from exc
        return _transfer_result(existing)
