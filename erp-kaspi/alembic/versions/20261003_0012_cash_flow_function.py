"""Cash-flow function for §13 «КУДА ДЕЛИСЬ МОИ ДЕНЬГИ?»

Revision ID: 0012_cash_flow_function
Revises: 0011_idempotency_per_ip
Create Date: 2026-10-03

Implements FINANCE-OPS-001 §13 — the cash-flow breakdown function
cash_flow(ip_id, period_start, period_end) with the fully specified formula:

  START BALANCE
  + SALES + OTHER INFLOWS + PAYOUTS
  - MATERIALS - PAYROLL - SUPPLIERS - COMMISSIONS - REFUNDS
  - OPERATING EXPENSES - TRANSFERS OUT
  = ENDING BALANCE

Rule: transfer ≠ expense (transfers shown separately).

Only POSTED operations are counted (§16). This closes the objectively-proven
gap found in docs/REPORT8_CONTROL_CHECK_2026-10-03.md §3.1.

database/schema.sql stays the source of truth (ADR-011); the function body
is copied verbatim from schema.sql §8.x.
"""

from __future__ import annotations

from alembic import op

revision = "0012_cash_flow_function"
down_revision = "0011_idempotency_per_ip"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cash_flow(
            p_ip_id INT,
            p_period_start DATE,
            p_period_end DATE
        )
        RETURNS TABLE (
            ip_id INT,
            period_start DATE,
            period_end DATE,
            start_balance NUMERIC(15,2),
            sales NUMERIC(15,2),
            other_inflows NUMERIC(15,2),
            payouts NUMERIC(15,2),
            materials NUMERIC(15,2),
            payroll NUMERIC(15,2),
            suppliers NUMERIC(15,2),
            commissions NUMERIC(15,2),
            refunds NUMERIC(15,2),
            operating_expenses NUMERIC(15,2),
            transfers_out NUMERIC(15,2),
            ending_balance NUMERIC(15,2)
        )
        LANGUAGE sql STABLE AS $$
            WITH period_ops AS (
                SELECT o.amount, ot.code, ot.kind
                FROM operations o
                JOIN operation_types ot ON ot.id = o.type_id
                WHERE o.ip_id = p_ip_id
                  AND o.status = 'POSTED'
                  AND o.operation_date >= p_period_start
                  AND o.operation_date <= p_period_end
            ),
            start_bal AS (
                SELECT coalesce(sum(o.amount), 0)::NUMERIC(15,2) AS bal
                FROM operations o
                WHERE o.ip_id = p_ip_id
                  AND o.status = 'POSTED'
                  AND o.operation_date < p_period_start
            )
            SELECT
                p_ip_id,
                p_period_start,
                p_period_end,
                (SELECT bal FROM start_bal),
                coalesce(sum(amount) FILTER (WHERE code IN ('SALE_KASPI_SHOP', 'SALE_KASPI_PAY')), 0)::NUMERIC(15,2),
                coalesce(sum(amount) FILTER (WHERE kind = 'INCOME' AND code NOT IN ('SALE_KASPI_SHOP', 'SALE_KASPI_PAY')), 0)::NUMERIC(15,2),
                coalesce((
                    SELECT sum(o.amount)::NUMERIC(15,2)
                    FROM operations o
                    JOIN kaspi_payouts kp ON kp.operation_id = o.id
                    WHERE o.ip_id = p_ip_id
                      AND o.status = 'POSTED'
                      AND o.operation_date >= p_period_start
                      AND o.operation_date <= p_period_end
                ), 0),
                coalesce(sum(-amount) FILTER (WHERE code = 'EXPENSE_MATERIAL'), 0)::NUMERIC(15,2),
                coalesce(sum(-amount) FILTER (WHERE code = 'SALARY_PAYMENT'), 0)::NUMERIC(15,2),
                0::NUMERIC(15,2),
                coalesce(sum(-amount) FILTER (WHERE code = 'EXPENSE_KASPI_FEE'), 0)::NUMERIC(15,2),
                0::NUMERIC(15,2),
                coalesce(sum(-amount) FILTER (WHERE kind = 'EXPENSE'
                    AND code NOT IN ('EXPENSE_MATERIAL', 'SALARY_PAYMENT', 'EXPENSE_KASPI_FEE')), 0)::NUMERIC(15,2),
                coalesce(sum(-amount) FILTER (WHERE code = 'TRANSFER_OUT'), 0)::NUMERIC(15,2),
                (SELECT bal FROM start_bal)
                + coalesce(sum(amount) FILTER (WHERE kind = 'INCOME'), 0)
                + coalesce(sum(amount) FILTER (WHERE code = 'TRANSFER_IN'), 0)
                - coalesce(sum(-amount) FILTER (WHERE kind = 'EXPENSE'), 0)
                - coalesce(sum(-amount) FILTER (WHERE code = 'TRANSFER_OUT'), 0)
            FROM period_ops;
        $$;
        """
    )
    op.execute(
        """
        COMMENT ON FUNCTION cash_flow(INT, DATE, DATE) IS
        '§13 FINANCE-OPS-001 «КУДА ДЕЛИСЬ МОИ ДЕНЬГИ?». Cash-flow breakdown with the specified formula. transfer ≠ expense.';
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS cash_flow(INT, DATE, DATE);")
