"""Add accrual_date to operations + settlement_links table (P1)

Revision ID: 0015_accrual_accounting
Revises: 0014_reconciliation_tolerance
Create Date: 2026-10-03

Implements accrual accounting foundation (BOOK_SYNTHESIS_2026-10-03, PHASE 4):

1. `operations.accrual_date DATE NULL`:
   - P&L recognition date (when economic event occurred)
   - NULL = pure cash movement, never in P&L (transfers, etc.)
   - `operation_date` remains the payment/cash-movement date
   - P&L aggregates by COALESCE(accrual_date, operation_date)
   - Cash Flow aggregates by operation_date

2. `settlement_links` table:
   - Many-to-many accrual↔payment matching (proven necessary by
     ACCRUAL_MODEL_DESIGN_2026-10-03 — single-row model fails 5/8 edge cases)
   - Stores relationship assertions, not money
   - paid/remaining always DERIVED via SUM(allocated_amount), never stored

3. Security fixes (ACCRUAL_SECURITY_REVIEW_2026-10-03):
   - C-1: Add accrual_date to POSTED immutability triggers
   - H-3: Split migration (ADD COLUMN first, backfill separately)

Backfill policy (PHASE 3):
- accrual_date = operation_date WHERE affects_pnl=TRUE AND status='POSTED'
- Transfers and non-P&L operations keep NULL (semantics-preserving, not reconstruction)
- Historical settlement_links NOT reconstructed (would be invented data)

database/schema.sql stays the source of truth (ADR-011).
"""

from __future__ import annotations

from alembic import op

revision = "0015_accrual_accounting"
down_revision = "0014_reconciliation_tolerance"
branch_labels = None
depends_on = None

# Full fn_protect_posted_ledger_operation body — must match schema.sql exactly
# (test_migration_function_matches_schema). Defined at module level with no
# indentation, same style as 0007.
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
       NEW.accrual_date IS DISTINCT FROM OLD.accrual_date OR
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
    # 1. Add accrual_date column (NULLABLE, no default — PG11+ metadata-only)
    op.execute(
        """
        ALTER TABLE operations
        ADD COLUMN IF NOT EXISTS accrual_date DATE NULL;

        COMMENT ON COLUMN operations.accrual_date IS
        'P&L recognition date (accrual basis). NULL = pure cash movement, excluded from P&L. '
        'P&L aggregates by COALESCE(accrual_date, operation_date); '
        'Cash Flow aggregates by operation_date. (BOOK_SYNTHESIS_2026-10-03)';
        """
    )

    # 2. Create settlement_links table (many-to-many accrual↔payment)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS settlement_links (
            id                  BIGSERIAL       PRIMARY KEY,
            ip_id               BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
            accrual_operation_id BIGINT         NOT NULL REFERENCES operations (id) ON DELETE RESTRICT,
            payment_operation_id BIGINT         NOT NULL REFERENCES operations (id) ON DELETE RESTRICT,
            allocated_amount    NUMERIC(15,2)   NOT NULL,
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

            CONSTRAINT chk_settlement_links_amount_positive CHECK (allocated_amount > 0),
            CONSTRAINT chk_settlement_links_no_self_link CHECK (
                accrual_operation_id <> payment_operation_id
            )
        );

        COMMENT ON TABLE settlement_links IS
        'Many-to-many accrual↔payment matching. Stores relationship assertions, not money. '
        'Paid/remaining amounts are always DERIVED via SUM(allocated_amount), never stored. '
        '(ACCRUAL_MODEL_DESIGN_2026-10-03)';

        CREATE INDEX IF NOT EXISTS ix_settlement_links_accrual
            ON settlement_links (accrual_operation_id);
        CREATE INDEX IF NOT EXISTS ix_settlement_links_payment
            ON settlement_links (payment_operation_id);
        CREATE INDEX IF NOT EXISTS ix_settlement_links_ip
            ON settlement_links (ip_id);
        """
    )

    # 3. C-1 FIX: Add accrual_date to POSTED immutability triggers
    # The triggers use explicit column lists — new column would be mutable otherwise
    # NOTE: Full function body from schema.sql (includes transfer logic + accrual_date)
    op.execute(
        """
        -- Update fn_protect_posted_operation to include accrual_date
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
                    NEW.accrual_date IS DISTINCT FROM OLD.accrual_date OR
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
                        'transfer operation % cannot be reversed independently; use a paired transfer correction workflow.',
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
        $$;
        """
    )

    # 3b. C-1 FIX (continued): fn_protect_posted_ledger_operation also needs accrual_date
    # Uses module-level constant to match schema.sql exactly (no indentation)
    op.execute(PROTECT_POSTED_LEDGER_FUNCTION)

    # 4. Backfill (separate step per H-3 — batched to avoid long locks)
    # Only for affects_pnl=TRUE + POSTED (semantics-preserving)
    op.execute(
        """
        UPDATE operations o
        SET accrual_date = o.operation_date
        FROM operation_types t
        WHERE o.type_id = t.id
          AND t.affects_pnl = TRUE
          AND o.status = 'POSTED'
          AND o.accrual_date IS NULL;
        """
    )

    # 5. Create v_counterparty_balances view (AR/AP Tier 1)
    op.execute(
        """
        CREATE OR REPLACE VIEW v_counterparty_balances AS
        SELECT
            o.ip_id,
            o.counterparty_id,
            c.name AS counterparty_name,
            COALESCE(
                SUM(o.amount) FILTER (WHERE o.accrual_date IS NOT NULL),
                0
            )::NUMERIC(15,2) AS total_accrued,
            COALESCE(
                SUM(o.amount) FILTER (WHERE o.accrual_date IS NULL),
                0
            )::NUMERIC(15,2) AS total_settled,
            (
                COALESCE(SUM(o.amount) FILTER (WHERE o.accrual_date IS NOT NULL), 0) -
                COALESCE(SUM(o.amount) FILTER (WHERE o.accrual_date IS NULL), 0)
            )::NUMERIC(15,2) AS balance
        FROM operations o
        LEFT JOIN counterparties c ON c.id = o.counterparty_id
        WHERE o.counterparty_id IS NOT NULL
          AND o.status = 'POSTED'
        GROUP BY o.ip_id, o.counterparty_id, c.name;
        """
    )


def downgrade() -> None:
    # H-3: Downgrade loses manual accrual_date corrections (documented)
    # Order matters: revert dependent views BEFORE dropping the column
    op.execute(
        """
        -- Revert v_ip_financial_summary to 0014 version (no accrual_date)
        CREATE OR REPLACE VIEW v_ip_financial_summary AS
        SELECT
            ip.id                                       AS ip_id,
            ip.name                                     AS ip_name,
            ip.bin                                      AS ip_bin,
            date_trunc('month', o.operation_date)::DATE AS period_month,
            coalesce(sum(o.amount) FILTER (WHERE ot.kind = 'INCOME'),  0)::NUMERIC(15,2)         AS total_income,
            coalesce(sum(-o.amount) FILTER (WHERE ot.kind = 'EXPENSE'), 0)::NUMERIC(15,2)        AS total_expense,
            coalesce(sum(o.amount) FILTER (WHERE ot.affects_pnl), 0)::NUMERIC(15,2)              AS net_profit,
            coalesce(sum(o.amount) FILTER (WHERE ot.code = 'SALE_KASPI_SHOP'), 0)::NUMERIC(15,2) AS revenue_kaspi_shop,
            coalesce(sum(o.amount) FILTER (WHERE ot.code = 'SALE_KASPI_PAY'),  0)::NUMERIC(15,2) AS revenue_kaspi_pay,
            coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'EXPENSE_MATERIAL'),  0)::NUMERIC(15,2) AS expense_material,
            coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'EXPENSE_KASPI_FEE'), 0)::NUMERIC(15,2) AS expense_kaspi_fee,
            coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'SALARY_PAYMENT'),    0)::NUMERIC(15,2) AS expense_salary,
            coalesce(sum(o.amount) FILTER (WHERE ot.code = 'TRANSFER_IN'),   0)::NUMERIC(15,2)   AS transfers_in,
            coalesce(sum(-o.amount) FILTER (WHERE ot.code = 'TRANSFER_OUT'), 0)::NUMERIC(15,2)   AS transfers_out,
            count(*)                                                                             AS operations_count
        FROM ip_entities ip
        JOIN operations      o  ON o.ip_id = ip.id AND o.status = 'POSTED'
        JOIN operation_types ot ON ot.id = o.type_id
        GROUP BY ip.id, ip.name, ip.bin, date_trunc('month', o.operation_date);

        DROP VIEW IF EXISTS v_counterparty_balances;
        DROP TABLE IF EXISTS settlement_links;
        ALTER TABLE operations DROP COLUMN IF EXISTS accrual_date;
        -- Note: fn_protect_posted_operation reverts to previous version via schema.sql
        """
    )
