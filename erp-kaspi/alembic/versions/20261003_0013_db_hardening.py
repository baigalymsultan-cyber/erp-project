"""DB hardening: payroll overpayment guard + FK indexes (P2-1, P2-2)

Revision ID: 0013_db_hardening
Revises: 0012_cash_flow_function
Create Date: 2026-10-03

P2-1: Adds fn_payroll_payment_no_overpay() trigger on payroll_payments
(BEFORE INSERT OR UPDATE). Ensures SUM(POSTED payments) <=
payroll_summary.total_calculated. The service already pays exactly once,
but this closes the direct-SQL / future-code overpayment vector.

P2-2: Adds 8 indexes on FK columns in financial paths that were missing
them (slow JOINs, slow RESTRICT checks):
- kaspi_payouts(operation_id), kaspi_payouts(raw_payload_id)
- payroll_payments(operation_id)
- transfers(from_account_id), transfers(to_account_id),
  transfers(outbound_operation_id), transfers(inbound_operation_id)
- payroll_summary(ip_id)

database/schema.sql stays the source of truth (ADR-011).
"""

from __future__ import annotations

from alembic import op

revision = "0013_db_hardening"
down_revision = "0012_cash_flow_function"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # P2-1: overpayment guard function + trigger
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_payroll_payment_no_overpay()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_total NUMERIC(15,2);
            v_paid NUMERIC(15,2);
        BEGIN
            IF NEW.status <> 'POSTED' THEN
                RETURN NEW;
            END IF;
            SELECT total_calculated INTO v_total
            FROM payroll_summary WHERE id = NEW.payroll_summary_id;
            IF NOT FOUND THEN
                RETURN NEW;
            END IF;
            SELECT coalesce(sum(amount), 0) INTO v_paid
            FROM payroll_payments
            WHERE payroll_summary_id = NEW.payroll_summary_id
              AND status = 'POSTED'
              AND id IS DISTINCT FROM NEW.id;
            IF v_paid + NEW.amount > v_total THEN
                RAISE EXCEPTION
                    'payroll overpayment blocked: summary % total is %, already paid %, new payment %',
                    NEW.payroll_summary_id, v_total, v_paid, NEW.amount
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_payroll_payments_no_overpay ON payroll_payments;
        CREATE TRIGGER trg_payroll_payments_no_overpay
            BEFORE INSERT OR UPDATE ON payroll_payments
            FOR EACH ROW EXECUTE FUNCTION fn_payroll_payment_no_overpay();
        """
    )
    # P2-2: FK indexes
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_kaspi_payouts_operation "
        "ON kaspi_payouts (operation_id) WHERE operation_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_kaspi_payouts_raw_payload "
        "ON kaspi_payouts (raw_payload_id) WHERE raw_payload_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_payroll_payments_operation "
        "ON payroll_payments (operation_id) WHERE operation_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfers_from_account "
        "ON transfers (from_account_id) WHERE from_account_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfers_to_account "
        "ON transfers (to_account_id) WHERE to_account_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfers_outbound_op "
        "ON transfers (outbound_operation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfers_inbound_op "
        "ON transfers (inbound_operation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_payroll_summary_ip ON payroll_summary (ip_id);"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_payroll_payments_no_overpay ON payroll_payments;")
    op.execute("DROP FUNCTION IF EXISTS fn_payroll_payment_no_overpay();")
    for idx in [
        "idx_kaspi_payouts_operation",
        "idx_kaspi_payouts_raw_payload",
        "idx_payroll_payments_operation",
        "idx_transfers_from_account",
        "idx_transfers_to_account",
        "idx_transfers_outbound_op",
        "idx_transfers_inbound_op",
        "idx_payroll_summary_ip",
    ]:
        op.execute(f"DROP INDEX IF EXISTS {idx};")
