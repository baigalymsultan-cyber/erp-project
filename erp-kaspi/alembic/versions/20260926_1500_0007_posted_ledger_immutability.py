"""posted ledger immutability for non-transfer operations (FIN-OPS-0001)

Revision ID: 0007_posted_ledger_immutability
Revises: 0006_transfer_pair_integrity
Create Date: 2026-09-26

FINANCE-OPS-001 §7: after an operation is posted, amount, direction (type),
legal entity (ip_id), account, source/document identity and actor must not be
changed by UPDATE. Before this revision only TRANSFER legs were protected in
the database (fn_protect_posted_operation, ADR-015); SALE / EXPENSE / SALARY
rows could be rewritten by the application role.

This revision adds a SEPARATE trigger so that the transfer guard (ADR-015,
commit b9dd921) is not modified. It changes no data, so no pre-check is
needed. The current reverse_operation() status flip (POSTED -> REVERSED) keeps
working; the compensating-reversal model is decided in ADR-016 (BLOCKED).

database/schema.sql stays the source of truth (ADR-011); the function body
below must stay byte-identical to schema.sql, which
tests/test_ledger_immutability.py::test_migration_function_matches_schema
enforces.
"""

from __future__ import annotations

from alembic import op

revision = "0007_posted_ledger_immutability"
down_revision = "0006_transfer_pair_integrity"
branch_labels = None
depends_on = None

PROTECT_POSTED_LEDGER_FUNCTION = """
CREATE OR REPLACE FUNCTION fn_protect_posted_ledger_operation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM operation_types WHERE id = OLD.type_id AND kind = 'TRANSFER'
    ) THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'DRAFT' THEN
        IF NEW.status NOT IN ('DRAFT', 'POSTED') THEN
            RAISE EXCEPTION
                'invalid operation status transition from % to % (FIN-OPS-0001).',
                OLD.status, NEW.status USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id OR
       NEW.ip_id IS DISTINCT FROM OLD.ip_id OR
       NEW.type_id IS DISTINCT FROM OLD.type_id OR
       NEW.category_id IS DISTINCT FROM OLD.category_id OR
       NEW.counterparty_id IS DISTINCT FROM OLD.counterparty_id OR
       NEW.amount IS DISTINCT FROM OLD.amount OR
       NEW.operation_date IS DISTINCT FROM OLD.operation_date OR
       NEW.external_id IS DISTINCT FROM OLD.external_id OR
       NEW.related_order_id IS DISTINCT FROM OLD.related_order_id OR
       NEW.related_operation_id IS DISTINCT FROM OLD.related_operation_id OR
       NEW.account_id IS DISTINCT FROM OLD.account_id OR
       NEW.import_fingerprint IS DISTINCT FROM OLD.import_fingerprint OR
       NEW.created_by IS DISTINCT FROM OLD.created_by OR
       NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'operation % is % and its financial fields are immutable; '
            'use reversal / correction instead of UPDATE (FIN-OPS-0001, FINANCE-OPS-001 §7).',
            OLD.id, OLD.status USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT (OLD.status = 'POSTED' AND NEW.status = 'REVERSED') THEN
        RAISE EXCEPTION
            'invalid operation status transition from % to % (FIN-OPS-0001).',
            OLD.status, NEW.status USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute(PROTECT_POSTED_LEDGER_FUNCTION)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_operations_protect_posted_ledger ON public.operations"
    )
    op.execute(
        "CREATE TRIGGER trg_operations_protect_posted_ledger "
        "BEFORE UPDATE ON public.operations "
        "FOR EACH ROW EXECUTE FUNCTION fn_protect_posted_ledger_operation()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_operations_protect_posted_ledger ON public.operations"
    )
    op.execute("DROP FUNCTION IF EXISTS fn_protect_posted_ledger_operation()")
