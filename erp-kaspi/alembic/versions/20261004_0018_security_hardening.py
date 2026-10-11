"""Security hardening: settlement trigger bypass + allocation race + payroll IP backfill.

Revision ID: 0018_security_hardening
Revises: 0017_accounts_bank_code
Create Date: 2026-10-04

1. SEC-HIGH: fn_settlement_links_immutable() honored the
   app.allow_financial_delete GUC without the erp_migrator membership gate
   that fn_forbid_delete() (TD-003) requires. Any role can SET a custom GUC,
   so any erp_app session could UPDATE/DELETE settlement_links. Fixed by
   copying the pg_has_role gate verbatim.

2. DI-003 race: fn_settlement_links_integrity() computed SUM(allocated_amount)
   without locking the accrual operation row — two concurrent transactions
   could both pass the cap. Added FOR UPDATE on the accrual SELECT to
   serialize concurrent link inserts per accrual (no isolation-level change).

3. SEC-002(b): backfill payroll_summary.ip_id from the employee's current
   assignment where NULL. Historical assignment is unknowable — current is
   the documented best-effort; the API now pins authorization to the frozen
   summary ip_id instead of the live employee assignment.
"""

from __future__ import annotations

from alembic import op

revision = "0018_security_hardening"
down_revision = "0017_accounts_bank_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Close the immutability bypass: require erp_migrator membership,
    #    exactly like fn_forbid_delete() (TD-003).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_settlement_links_immutable()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_hatch_role TEXT := 'erp_migrator';
        BEGIN
            IF coalesce(current_setting('app.allow_financial_delete', TRUE), 'off') = 'on'
               AND coalesce((
                       SELECT true
                       FROM pg_roles r
                       WHERE r.rolname = v_hatch_role
                         AND pg_has_role(current_user, r.rolname, 'member')
                   ), false) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION
                'settlement_links: rows are immutable (DI-005). '
                'Create a new link for corrections, do not mutate.'
                USING ERRCODE = 'check_violation';
            RETURN NULL;
        END;
        $$;
        """
    )
    # 2. Serialize concurrent allocation inserts per accrual operation.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_settlement_links_integrity()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_accrual_ip BIGINT;
            v_payment_ip BIGINT;
            v_accrual_status VARCHAR(20);
            v_payment_status VARCHAR(20);
            v_accrual_date DATE;
            v_accrual_amount NUMERIC(15,2);
            v_total_allocated NUMERIC(15,2);
        BEGIN
            -- FOR UPDATE: concurrent link inserts against the same accrual
            -- must serialize, otherwise two transactions can both read a
            -- stale SUM and jointly exceed the allocation cap (DI-003).
            SELECT status, ip_id, accrual_date, amount
            INTO v_accrual_status, v_accrual_ip, v_accrual_date, v_accrual_amount
            FROM operations WHERE id = NEW.accrual_operation_id
            FOR UPDATE;

            SELECT status, ip_id
            INTO v_payment_status, v_payment_ip
            FROM operations WHERE id = NEW.payment_operation_id;

            IF v_accrual_status IS DISTINCT FROM 'POSTED' THEN
                RAISE EXCEPTION
                    'settlement_links: accrual operation % is %, must be POSTED (DI-004)',
                    NEW.accrual_operation_id, v_accrual_status
                    USING ERRCODE = 'check_violation';
            END IF;

            IF v_payment_status IS DISTINCT FROM 'POSTED' THEN
                RAISE EXCEPTION
                    'settlement_links: payment operation % is %, must be POSTED (DI-004)',
                    NEW.accrual_operation_id, v_payment_status
                    USING ERRCODE = 'check_violation';
            END IF;

            IF v_accrual_ip IS DISTINCT FROM NEW.ip_id OR
               v_payment_ip IS DISTINCT FROM NEW.ip_id THEN
                RAISE EXCEPTION
                    'settlement_links: cross-IP link blocked (DI-001). '
                    'link ip=%, accrual ip=%, payment ip=%',
                    NEW.ip_id, v_accrual_ip, v_payment_ip
                    USING ERRCODE = 'check_violation';
            END IF;

            IF v_accrual_date IS NULL THEN
                RAISE EXCEPTION
                    'settlement_links: accrual operation % has NULL accrual_date, '
                    'cannot be an accrual (DI-002)',
                    NEW.accrual_operation_id
                    USING ERRCODE = 'check_violation';
            END IF;

            SELECT COALESCE(SUM(allocated_amount), 0)
            INTO v_total_allocated
            FROM settlement_links
            WHERE accrual_operation_id = NEW.accrual_operation_id
              AND id IS DISTINCT FROM NEW.id;

            IF v_total_allocated + NEW.allocated_amount > ABS(v_accrual_amount) THEN
                RAISE EXCEPTION
                    'settlement_links: over-allocation blocked (DI-003). '
                    'accrual %=%, already allocated %, new % would exceed',
                    NEW.accrual_operation_id, ABS(v_accrual_amount),
                    v_total_allocated, NEW.allocated_amount
                    USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    # 3. Backfill frozen IP on payroll summaries (SEC-002(b)).
    op.execute(
        """
        UPDATE payroll_summary ps
        SET ip_id = e.assigned_ip_id
        FROM employees e
        WHERE ps.ip_id IS NULL
          AND ps.employee_id = e.id
          AND e.assigned_ip_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    # Restore the pre-hardening trigger bodies (schema.sql is the source of
    # truth for fresh databases; keep them in sync manually).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_settlement_links_immutable()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF coalesce(current_setting('app.allow_financial_delete', TRUE), 'off') = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION
                'settlement_links: rows are immutable (DI-005). '
                'Create a new link for corrections, do not mutate.'
                USING ERRCODE = 'check_violation';
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_settlement_links_integrity()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_accrual_ip BIGINT;
            v_payment_ip BIGINT;
            v_accrual_status VARCHAR(20);
            v_payment_status VARCHAR(20);
            v_accrual_date DATE;
            v_accrual_amount NUMERIC(15,2);
            v_total_allocated NUMERIC(15,2);
        BEGIN
            SELECT status, ip_id, accrual_date, amount
            INTO v_accrual_status, v_accrual_ip, v_accrual_date, v_accrual_amount
            FROM operations WHERE id = NEW.accrual_operation_id;

            SELECT status, ip_id
            INTO v_payment_status, v_payment_ip
            FROM operations WHERE id = NEW.payment_operation_id;

            IF v_accrual_status IS DISTINCT FROM 'POSTED' THEN
                RAISE EXCEPTION
                    'settlement_links: accrual operation % is %, must be POSTED (DI-004)',
                    NEW.accrual_operation_id, v_accrual_status
                    USING ERRCODE = 'check_violation';
            END IF;

            IF v_payment_status IS DISTINCT FROM 'POSTED' THEN
                RAISE EXCEPTION
                    'settlement_links: payment operation % is %, must be POSTED (DI-004)',
                    NEW.accrual_operation_id, v_payment_status
                    USING ERRCODE = 'check_violation';
            END IF;

            IF v_accrual_ip IS DISTINCT FROM NEW.ip_id OR
               v_payment_ip IS DISTINCT FROM NEW.ip_id THEN
                RAISE EXCEPTION
                    'settlement_links: cross-IP link blocked (DI-001). '
                    'link ip=%, accrual ip=%, payment ip=%',
                    NEW.ip_id, v_accrual_ip, v_payment_ip
                    USING ERRCODE = 'check_violation';
            END IF;

            IF v_accrual_date IS NULL THEN
                RAISE EXCEPTION
                    'settlement_links: accrual operation % has NULL accrual_date, '
                    'cannot be an accrual (DI-002)',
                    NEW.accrual_operation_id
                    USING ERRCODE = 'check_violation';
            END IF;

            SELECT COALESCE(SUM(allocated_amount), 0)
            INTO v_total_allocated
            FROM settlement_links
            WHERE accrual_operation_id = NEW.accrual_operation_id
              AND id IS DISTINCT FROM NEW.id;

            IF v_total_allocated + NEW.allocated_amount > ABS(v_accrual_amount) THEN
                RAISE EXCEPTION
                    'settlement_links: over-allocation blocked (DI-003). '
                    'accrual %=%, already allocated %, new % would exceed',
                    NEW.accrual_operation_id, ABS(v_accrual_amount),
                    v_total_allocated, NEW.allocated_amount
                    USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    # Backfill is not reversed (data repair, no schema change).
