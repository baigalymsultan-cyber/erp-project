from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_transfer_ledger"
down_revision = "0004_auth_rate_events"
branch_labels = None
depends_on = None


def _constraint_exists(bind, table_name: str, constraint_name: str) -> bool:
    return bool(
        bind.scalar(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conrelid = to_regclass(:table_name) AND conname = :constraint_name"
            ),
            {"table_name": f"public.{table_name}", "constraint_name": constraint_name},
        )
    )


def _index_exists(bind, index_name: str) -> bool:
    return bool(
        bind.scalar(
            sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"),
            {"name": index_name},
        )
    )


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    return bool(
        bind.scalar(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table_name "
                "AND column_name = :column_name"
            ),
            {"table_name": table_name, "column_name": column_name},
        )
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    bind = op.get_bind()
    mismatched_accounts = bind.scalar(
        sa.text(
            "SELECT count(*) FROM operations o "
            "LEFT JOIN accounts a ON a.id = o.account_id "
            "WHERE o.account_id IS NOT NULL AND (a.id IS NULL OR a.ip_id <> o.ip_id)"
        )
    )
    if mismatched_accounts:
        raise RuntimeError(
            f"Cannot enforce operation account/IP ownership: {mismatched_accounts} rows conflict"
        )

    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_id_ip ON public.accounts (id, ip_id)")

    for column_name in ("from_account_id", "to_account_id", "idempotency_key"):
        if not _column_exists(bind, "transfers", column_name):
            column_type = "VARCHAR(255)" if column_name == "idempotency_key" else "BIGINT"
            op.execute(
                f"ALTER TABLE public.transfers ADD COLUMN {column_name} {column_type} NULL"
            )

    if not _constraint_exists(bind, "operations", "fk_operations_account_ip"):
        op.execute(
            "ALTER TABLE public.operations ADD CONSTRAINT fk_operations_account_ip "
            "FOREIGN KEY (account_id, ip_id) REFERENCES public.accounts (id, ip_id) "
            "ON DELETE RESTRICT NOT VALID"
        )
    op.execute("ALTER TABLE public.operations VALIDATE CONSTRAINT fk_operations_account_ip")
    if _constraint_exists(bind, "operations", "operations_account_id_fkey"):
        op.drop_constraint("operations_account_id_fkey", "operations", type_="foreignkey")

    transfer_foreign_keys = (
        (
            "fk_transfers_from_account_ip",
            "from_account_id, from_ip_id",
        ),
        (
            "fk_transfers_to_account_ip",
            "to_account_id, to_ip_id",
        ),
    )
    for constraint_name, local_columns in transfer_foreign_keys:
        if not _constraint_exists(bind, "transfers", constraint_name):
            op.execute(
                f"ALTER TABLE public.transfers ADD CONSTRAINT {constraint_name} "
                f"FOREIGN KEY ({local_columns}) REFERENCES public.accounts (id, ip_id) "
                "ON DELETE RESTRICT NOT VALID"
            )
        op.execute(
            f"ALTER TABLE public.transfers VALIDATE CONSTRAINT {constraint_name}"
        )

    transfer_checks = (
        (
            "chk_transfers_accounts_distinct",
            "from_account_id IS NULL OR to_account_id IS NULL "
            "OR from_account_id <> to_account_id",
        ),
        (
            "chk_transfers_keyed_pair",
            "idempotency_key IS NULL OR (length(btrim(idempotency_key)) > 0 "
            "AND from_account_id IS NOT NULL AND to_account_id IS NOT NULL "
            "AND created_by IS NOT NULL AND outbound_operation_id IS NOT NULL "
            "AND inbound_operation_id IS NOT NULL)",
        ),
    )
    for constraint_name, expression in transfer_checks:
        if not _constraint_exists(bind, "transfers", constraint_name):
            op.create_check_constraint(constraint_name, "transfers", expression)

    incomplete_posted_transfers = bind.scalar(
        sa.text(
            "SELECT count(*) FROM public.transfers WHERE status = 'POSTED' "
            "AND (outbound_operation_id IS NULL OR inbound_operation_id IS NULL)"
        )
    )
    posted_legs_check = "chk_transfers_posted_has_legs"
    if _constraint_exists(bind, "transfers", posted_legs_check):
        if not incomplete_posted_transfers:
            op.execute(
                "ALTER TABLE public.transfers VALIDATE CONSTRAINT chk_transfers_posted_has_legs"
            )
    elif incomplete_posted_transfers:
        op.execute(
            "ALTER TABLE public.transfers ADD CONSTRAINT chk_transfers_posted_has_legs "
            "CHECK (status <> 'POSTED' OR "
            "(outbound_operation_id IS NOT NULL AND inbound_operation_id IS NOT NULL)) "
            "NOT VALID"
        )
    else:
        op.create_check_constraint(
            posted_legs_check,
            "transfers",
            "status <> 'POSTED' OR "
            "(outbound_operation_id IS NOT NULL AND inbound_operation_id IS NOT NULL)",
        )

    if not _index_exists(bind, "uq_transfers_idempotency_key"):
        op.create_index(
            "uq_transfers_idempotency_key",
            "transfers",
            ["idempotency_key"],
            unique=True,
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_protect_posted_operation()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM operation_types WHERE id = OLD.type_id AND kind = 'TRANSFER'
            ) THEN
                IF OLD.status <> 'DRAFT' AND (
                    NEW.id IS DISTINCT FROM OLD.id OR
                    NEW.ip_id IS DISTINCT FROM OLD.ip_id OR
                    NEW.type_id IS DISTINCT FROM OLD.type_id OR
                    NEW.category_id IS DISTINCT FROM OLD.category_id OR
                    NEW.counterparty_id IS DISTINCT FROM OLD.counterparty_id OR
                    NEW.amount IS DISTINCT FROM OLD.amount OR
                    NEW.operation_date IS DISTINCT FROM OLD.operation_date OR
                    NEW.external_id IS DISTINCT FROM OLD.external_id OR
                    NEW.related_order_id IS DISTINCT FROM OLD.related_order_id OR
                    NEW.related_operation_id IS DISTINCT FROM OLD.related_operation_id OR
                    NEW.raw_payload_id IS DISTINCT FROM OLD.raw_payload_id OR
                    NEW.account_id IS DISTINCT FROM OLD.account_id OR
                    NEW.import_batch_id IS DISTINCT FROM OLD.import_batch_id OR
                    NEW.import_fingerprint IS DISTINCT FROM OLD.import_fingerprint OR
                    NEW.created_by IS DISTINCT FROM OLD.created_by
                ) THEN
                    RAISE EXCEPTION
                        'posted transfer operation % is immutable; '
                        'use a paired transfer correction workflow.',
                        OLD.id USING ERRCODE = 'check_violation';
                END IF;

                IF OLD.status = 'POSTED' AND NEW.status = 'REVERSED' THEN
                    RAISE EXCEPTION
                        'transfer operation % cannot be reversed independently; '
                        'use a paired transfer correction workflow.',
                        OLD.id USING ERRCODE = 'check_violation';
                END IF;

                IF NEW.status IS DISTINCT FROM OLD.status
                   AND NOT (OLD.status = 'DRAFT' AND NEW.status = 'POSTED') THEN
                    RAISE EXCEPTION
                        'invalid transfer operation status transition from % to %.',
                        OLD.status, NEW.status USING ERRCODE = 'check_violation';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_protect_posted_transfer()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.status = 'POSTED' AND (
                NEW.id IS DISTINCT FROM OLD.id OR
                NEW.from_ip_id IS DISTINCT FROM OLD.from_ip_id OR
                NEW.to_ip_id IS DISTINCT FROM OLD.to_ip_id OR
                NEW.from_account_id IS DISTINCT FROM OLD.from_account_id OR
                NEW.to_account_id IS DISTINCT FROM OLD.to_account_id OR
                NEW.amount IS DISTINCT FROM OLD.amount OR
                NEW.transfer_date IS DISTINCT FROM OLD.transfer_date OR
                NEW.reason IS DISTINCT FROM OLD.reason OR
                NEW.outbound_operation_id IS DISTINCT FROM OLD.outbound_operation_id OR
                NEW.inbound_operation_id IS DISTINCT FROM OLD.inbound_operation_id OR
                NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR
                NEW.status IS DISTINCT FROM OLD.status OR
                NEW.created_by IS DISTINCT FROM OLD.created_by
            ) THEN
                RAISE EXCEPTION
                    'posted transfer % is immutable; use a paired transfer correction workflow.',
                    OLD.id USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_operations_protect_posted ON public.operations")
    op.execute(
        "CREATE TRIGGER trg_operations_protect_posted BEFORE UPDATE ON public.operations "
        "FOR EACH ROW EXECUTE FUNCTION fn_protect_posted_operation()"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_transfers_protect_posted ON public.transfers")
    op.execute(
        "CREATE TRIGGER trg_transfers_protect_posted BEFORE UPDATE ON public.transfers "
        "FOR EACH ROW EXECUTE FUNCTION fn_protect_posted_transfer()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    bind = op.get_bind()
    keyed_rows = bind.scalar(
        sa.text(
            "SELECT count(*) FROM transfers WHERE from_account_id IS NOT NULL "
            "OR to_account_id IS NOT NULL OR idempotency_key IS NOT NULL"
        )
    )
    if keyed_rows:
        raise RuntimeError(
            "Refusing to remove transfer account/idempotency provenance while rows use it"
        )

    op.execute("DROP TRIGGER IF EXISTS trg_operations_protect_posted ON public.operations")
    op.execute("DROP TRIGGER IF EXISTS trg_transfers_protect_posted ON public.transfers")
    op.execute("DROP FUNCTION IF EXISTS fn_protect_posted_operation()")
    op.execute("DROP FUNCTION IF EXISTS fn_protect_posted_transfer()")
    op.execute("DROP INDEX IF EXISTS public.uq_transfers_idempotency_key")
    for constraint_name in (
        "chk_transfers_accounts_distinct",
        "chk_transfers_keyed_pair",
        "chk_transfers_posted_has_legs",
        "fk_transfers_from_account_ip",
        "fk_transfers_to_account_ip",
        "fk_operations_account_ip",
    ):
        for table_name in ("transfers", "operations"):
            if _constraint_exists(bind, table_name, constraint_name):
                constraint_type = "foreignkey" if constraint_name.startswith("fk_") else "check"
                op.drop_constraint(constraint_name, table_name, type_=constraint_type)
    op.execute(
        "ALTER TABLE public.transfers DROP COLUMN IF EXISTS idempotency_key, "
        "DROP COLUMN IF EXISTS to_account_id, DROP COLUMN IF EXISTS from_account_id"
    )
    if not _constraint_exists(bind, "operations", "operations_account_id_fkey"):
        op.create_foreign_key(
            "operations_account_id_fkey",
            "operations",
            "accounts",
            ["account_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.execute("DROP INDEX IF EXISTS public.uq_accounts_id_ip")
