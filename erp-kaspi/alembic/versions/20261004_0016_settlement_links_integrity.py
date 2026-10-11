"""Add settlement_links integrity triggers (DI-001..DI-005)

Revision ID: 0016_settlement_links_integrity
Revises: 0015_accrual_accounting
Create Date: 2026-10-04

Implements P1 data integrity gaps from GAP_P1_DATA_INTEGRITY_2026-10-04:

DI-001 (HIGH): Same-IP enforcement — settlement link cannot connect
    operations from different IPs. Prevents cross-IP data leak via AR/AP.

DI-002 (HIGH): Operation role validation — accrual_operation must have
    accrual_date IS NOT NULL (it's a real economic event). Payment operation
    must be a different operation (already enforced by no_self_link).

DI-003 (MEDIUM): Allocation sum cap — SUM(allocated_amount) for an accrual
    cannot exceed ABS(accrual.amount). Prevents over-allocation.

DI-004 (MEDIUM): POSTED status gate — both operations must be POSTED.
    Cannot link DRAFT operations.

DI-005 (LOW): Immutability — settlement_links rows cannot be UPDATE/DELETE.
    Links are assertions; corrections via new links, not mutation.
"""

from __future__ import annotations

from alembic import op

revision = "0016_settlement_links_integrity"
down_revision = "0015_accrual_accounting"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
            -- DI-004: Both operations must be POSTED
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
                    NEW.payment_operation_id, v_payment_status
                    USING ERRCODE = 'check_violation';
            END IF;

            -- DI-001: Same-IP enforcement
            IF v_accrual_ip IS DISTINCT FROM NEW.ip_id OR
               v_payment_ip IS DISTINCT FROM NEW.ip_id THEN
                RAISE EXCEPTION
                    'settlement_links: cross-IP link blocked (DI-001). '
                    'link ip=%, accrual ip=%, payment ip=%',
                    NEW.ip_id, v_accrual_ip, v_payment_ip
                    USING ERRCODE = 'check_violation';
            END IF;

            -- DI-002: Accrual operation must have accrual_date (real economic event)
            IF v_accrual_date IS NULL THEN
                RAISE EXCEPTION
                    'settlement_links: accrual operation % has NULL accrual_date, '
                    'cannot be an accrual (DI-002)',
                    NEW.accrual_operation_id
                    USING ERRCODE = 'check_violation';
            END IF;

            -- DI-003: Allocation sum cap
            SELECT COALESCE(SUM(allocated_amount), 0)
            INTO v_total_allocated
            FROM settlement_links
            WHERE accrual_operation_id = NEW.accrual_operation_id
              AND id IS DISTINCT FROM NEW.id;  -- exclude self on UPDATE (though UPDATE blocked)

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

        DROP TRIGGER IF EXISTS trg_settlement_links_integrity ON settlement_links;
        CREATE TRIGGER trg_settlement_links_integrity
            BEFORE INSERT OR UPDATE ON settlement_links
            FOR EACH ROW EXECUTE FUNCTION fn_settlement_links_integrity();

        -- DI-005: Immutability — no UPDATE/DELETE on settlement_links
        CREATE OR REPLACE FUNCTION fn_settlement_links_immutable()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- Allow test purge via app.allow_financial_delete
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

        DROP TRIGGER IF EXISTS trg_settlement_links_immutable ON settlement_links;
        CREATE TRIGGER trg_settlement_links_immutable
            BEFORE UPDATE OR DELETE ON settlement_links
            FOR EACH ROW EXECUTE FUNCTION fn_settlement_links_immutable();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_settlement_links_immutable ON settlement_links;
        DROP TRIGGER IF EXISTS trg_settlement_links_integrity ON settlement_links;
        DROP FUNCTION IF EXISTS fn_settlement_links_immutable();
        DROP FUNCTION IF EXISTS fn_settlement_links_integrity();
        """
    )
