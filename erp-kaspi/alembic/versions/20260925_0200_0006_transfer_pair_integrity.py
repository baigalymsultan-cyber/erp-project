from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_transfer_pair_integrity"
down_revision = "0005_transfer_ledger"
branch_labels = None
depends_on = None

_PAIR_VALIDITY_FUNCTION = """
CREATE OR REPLACE FUNCTION fn_transfer_ledger_pair_is_valid(p_transfer_id BIGINT)
RETURNS BOOLEAN
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1
        FROM transfers t
        JOIN operations outbound ON outbound.id = t.outbound_operation_id
        JOIN operation_types outbound_type ON outbound_type.id = outbound.type_id
        JOIN operations inbound ON inbound.id = t.inbound_operation_id
        JOIN operation_types inbound_type ON inbound_type.id = inbound.type_id
        WHERE t.id = p_transfer_id
          AND t.status = 'POSTED'
          AND t.from_ip_id <> t.to_ip_id
          AND t.from_account_id IS NOT NULL
          AND t.to_account_id IS NOT NULL
          AND t.idempotency_key IS NOT NULL
          AND length(btrim(t.idempotency_key)) > 0
          AND t.created_by IS NOT NULL
          AND outbound.status = 'POSTED'
          AND inbound.status = 'POSTED'
          AND outbound.ip_id = t.from_ip_id
          AND inbound.ip_id = t.to_ip_id
          AND outbound.account_id IS NOT DISTINCT FROM t.from_account_id
          AND inbound.account_id IS NOT DISTINCT FROM t.to_account_id
          AND outbound.amount = -t.amount
          AND inbound.amount = t.amount
          AND outbound.operation_date = t.transfer_date
          AND inbound.operation_date = t.transfer_date
          AND outbound.comment IS NOT DISTINCT FROM t.reason
          AND inbound.comment IS NOT DISTINCT FROM t.reason
          AND outbound.created_by = t.created_by
          AND inbound.created_by = t.created_by
          AND outbound.related_operation_id = inbound.id
          AND inbound.related_operation_id = outbound.id
          AND outbound_type.code = 'TRANSFER_OUT'
          AND outbound_type.direction = 'OUT'
          AND outbound_type.kind = 'TRANSFER'
          AND NOT outbound_type.affects_pnl
          AND inbound_type.code = 'TRANSFER_IN'
          AND inbound_type.direction = 'IN'
          AND inbound_type.kind = 'TRANSFER'
          AND NOT inbound_type.affects_pnl
          AND (
              SELECT count(*)
              FROM (
                  SELECT linked.outbound_operation_id AS operation_id FROM transfers linked
                  UNION ALL
                  SELECT linked.inbound_operation_id AS operation_id FROM transfers linked
              ) leg_refs
              WHERE leg_refs.operation_id = outbound.id
          ) = 1
          AND (
              SELECT count(*)
              FROM (
                  SELECT linked.outbound_operation_id AS operation_id FROM transfers linked
                  UNION ALL
                  SELECT linked.inbound_operation_id AS operation_id FROM transfers linked
              ) leg_refs
              WHERE leg_refs.operation_id = inbound.id
          ) = 1
    );
END;
$$
"""

_PAIR_VALIDATION_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION fn_validate_transfer_ledger_pair()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    target_transfer_id BIGINT;
    operation_code TEXT;
    operation_status TEXT;
    reference_count BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'transfers' THEN
        SELECT t.id INTO target_transfer_id
        FROM transfers t
        WHERE t.id = NEW.id AND t.status = 'POSTED';
        IF target_transfer_id IS NULL THEN
            RETURN NEW;
        END IF;
    ELSE
        SELECT ot.code, o.status INTO operation_code, operation_status
        FROM operations o
        JOIN operation_types ot ON ot.id = o.type_id
        WHERE o.id = NEW.id;
        IF NOT FOUND OR operation_status <> 'POSTED'
           OR operation_code NOT IN ('TRANSFER_OUT', 'TRANSFER_IN') THEN
            RETURN NEW;
        END IF;

        SELECT count(*), min(t.id) INTO reference_count, target_transfer_id
        FROM transfers t
        WHERE t.outbound_operation_id = NEW.id OR t.inbound_operation_id = NEW.id;
        IF reference_count <> 1 THEN
            RAISE EXCEPTION
                'posted transfer operation % must be linked to exactly one transfer',
                NEW.id USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF NOT fn_transfer_ledger_pair_is_valid(target_transfer_id) THEN
        RAISE EXCEPTION
            'posted transfer % does not have a matching ledger pair',
            target_transfer_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    bind = op.get_bind()
    op.execute("LOCK TABLE public.operations, public.transfers IN SHARE ROW EXCLUSIVE MODE")
    op.execute(sa.text(_PAIR_VALIDITY_FUNCTION))
    op.execute(sa.text(_PAIR_VALIDATION_TRIGGER_FUNCTION))

    posted_leg_check = bind.execute(
        sa.text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conrelid = to_regclass('public.transfers') "
            "AND conname = 'chk_transfers_posted_has_legs'"
        )
    ).scalar_one_or_none()
    if posted_leg_check is None:
        op.create_check_constraint(
            "chk_transfers_posted_has_legs",
            "transfers",
            "status <> 'POSTED' OR "
            "(outbound_operation_id IS NOT NULL AND inbound_operation_id IS NOT NULL)",
        )
    else:
        incomplete = bind.scalar(
            sa.text(
                "SELECT count(*) FROM public.transfers WHERE status = 'POSTED' "
                "AND (outbound_operation_id IS NULL OR inbound_operation_id IS NULL)"
            )
        )
        if incomplete:
            raise RuntimeError(
                f"Cannot validate chk_transfers_posted_has_legs: {incomplete} "
                "POSTED transfer rows have a missing ledger leg; reconcile them "
                "explicitly before deploying this migration."
            )
        if not posted_leg_check:
            op.execute(
                "ALTER TABLE public.transfers "
                "VALIDATE CONSTRAINT chk_transfers_posted_has_legs"
            )

    invalid_transfers = bind.scalar(
        sa.text(
            "SELECT count(*) FROM public.transfers t "
            "WHERE t.status = 'POSTED' "
            "AND NOT fn_transfer_ledger_pair_is_valid(t.id)"
        )
    )
    unlinked_legs = bind.scalar(
        sa.text(
            "SELECT count(*) FROM public.operations o "
            "JOIN public.operation_types ot ON ot.id = o.type_id "
            "WHERE o.status = 'POSTED' "
            "AND ot.code IN ('TRANSFER_OUT', 'TRANSFER_IN') "
            "AND (SELECT count(*) FROM public.transfers t "
            "WHERE t.outbound_operation_id = o.id OR t.inbound_operation_id = o.id) <> 1"
        )
    )
    if invalid_transfers or unlinked_legs:
        raise RuntimeError(
            "Cannot enforce transfer ledger-pair integrity: "
            f"{invalid_transfers} POSTED transfers are inconsistent and "
            f"{unlinked_legs} POSTED transfer operations are not linked to exactly "
            "one transfer. Reconcile the existing ledger explicitly; this migration "
            "will not synthesize financial entries."
        )

    op.execute("DROP TRIGGER IF EXISTS trg_transfers_validate_ledger_pair ON public.transfers")
    op.execute("DROP TRIGGER IF EXISTS trg_operations_validate_transfer_pair ON public.operations")
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_transfers_validate_ledger_pair "
        "AFTER INSERT OR UPDATE ON public.transfers "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION fn_validate_transfer_ledger_pair()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_operations_validate_transfer_pair "
        "AFTER INSERT OR UPDATE ON public.operations "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION fn_validate_transfer_ledger_pair()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("DROP TRIGGER IF EXISTS trg_operations_validate_transfer_pair ON public.operations")
    op.execute("DROP TRIGGER IF EXISTS trg_transfers_validate_ledger_pair ON public.transfers")
    op.execute("DROP FUNCTION IF EXISTS fn_validate_transfer_ledger_pair()")
    op.execute("DROP FUNCTION IF EXISTS fn_transfer_ledger_pair_is_valid(BIGINT)")
