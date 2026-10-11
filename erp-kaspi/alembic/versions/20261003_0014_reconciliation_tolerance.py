"""Fix v_kaspi_reconciliation tolerance (P1)

Revision ID: 0014_reconciliation_tolerance
Revises: 0013_db_hardening
Create Date: 2026-10-03

P1: v_kaspi_reconciliation.verdict used strict <> (zero tolerance) for
amount/commission/delivery comparisons, while the service
(reconcile_payout_full) uses ReconciliationRule.amount_tolerance (default
1.00 KZT). The same payout item could show MATCHED in the service result
and AMOUNT_MISMATCH in the view — inconsistent UX.

Fix: the view now uses abs(delta) > 1.00, matching the service default.

database/schema.sql stays the source of truth (ADR-011).
"""

from __future__ import annotations

from alembic import op

revision = "0014_reconciliation_tolerance"
down_revision = "0013_db_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW v_kaspi_reconciliation AS
        SELECT
            o.id                            AS order_id,
            o.ip_id,
            o.external_order_id,
            o.status                        AS order_status,
            o.total_amount                  AS order_amount,
            o.kaspi_commission              AS order_commission,
            o.delivery_fee                  AS order_delivery_fee,
            pi.id                           AS payout_item_id,
            pi.payout_id,
            p.external_payout_id,
            p.payout_date,
            p.status                        AS payout_status,
            pi.gross_amount                 AS payout_gross_amount,
            pi.commission_amount            AS payout_commission,
            pi.delivery_amount              AS payout_delivery,
            pi.refund_amount                AS payout_refund,
            pi.net_amount                   AS payout_net_amount,
            pi.item_type,
            pi.match_status,
            p.operation_id,
            op.amount                       AS operation_amount,
            op.operation_date,
            op.status                       AS operation_status,
            (o.total_amount - pi.gross_amount)::NUMERIC(15,2)         AS amount_delta,
            (o.kaspi_commission - pi.commission_amount)::NUMERIC(15,2) AS commission_delta,
            (o.delivery_fee - pi.delivery_amount)::NUMERIC(15,2)       AS delivery_delta,
            CASE
                WHEN pi.id IS NULL                                    THEN 'NO_PAYOUT'
                WHEN p.operation_id IS NULL                           THEN 'PAYOUT_NOT_IN_LEDGER'
                WHEN abs(o.total_amount - pi.gross_amount) > 1.00     THEN 'AMOUNT_MISMATCH'
                WHEN abs(o.kaspi_commission - pi.commission_amount) > 1.00 THEN 'COMMISSION_MISMATCH'
                WHEN abs(o.delivery_fee - pi.delivery_amount) > 1.00 THEN 'DELIVERY_MISMATCH'
                WHEN pi.refund_amount > 0                             THEN 'REFUND'
                ELSE 'OK'
            END                             AS verdict
        FROM orders o
        LEFT JOIN kaspi_payout_items pi ON pi.order_id = o.id
        LEFT JOIN kaspi_payouts      p  ON p.id  = pi.payout_id
        LEFT JOIN operations         op ON op.id = p.operation_id
        WHERE o.source IN ('KASPI_SHOP', 'KASPI_PAY');
        """
    )


def downgrade() -> None:
    # Revert to strict comparison (not recommended — kept for completeness)
    op.execute(
        """
        CREATE OR REPLACE VIEW v_kaspi_reconciliation AS
        SELECT
            o.id                            AS order_id,
            o.ip_id,
            o.external_order_id,
            o.status                        AS order_status,
            o.total_amount                  AS order_amount,
            o.kaspi_commission              AS order_commission,
            o.delivery_fee                  AS order_delivery_fee,
            pi.id                           AS payout_item_id,
            pi.payout_id,
            p.external_payout_id,
            p.payout_date,
            p.status                        AS payout_status,
            pi.gross_amount                 AS payout_gross_amount,
            pi.commission_amount            AS payout_commission,
            pi.delivery_amount              AS payout_delivery,
            pi.refund_amount                AS payout_refund,
            pi.net_amount                   AS payout_net_amount,
            pi.item_type,
            pi.match_status,
            p.operation_id,
            op.amount                       AS operation_amount,
            op.operation_date,
            op.status                       AS operation_status,
            (o.total_amount - pi.gross_amount)::NUMERIC(15,2)         AS amount_delta,
            (o.kaspi_commission - pi.commission_amount)::NUMERIC(15,2) AS commission_delta,
            (o.delivery_fee - pi.delivery_amount)::NUMERIC(15,2)       AS delivery_delta,
            CASE
                WHEN pi.id IS NULL                                    THEN 'NO_PAYOUT'
                WHEN p.operation_id IS NULL                           THEN 'PAYOUT_NOT_IN_LEDGER'
                WHEN o.total_amount <> pi.gross_amount                THEN 'AMOUNT_MISMATCH'
                WHEN o.kaspi_commission <> pi.commission_amount       THEN 'COMMISSION_MISMATCH'
                WHEN o.delivery_fee <> pi.delivery_amount             THEN 'DELIVERY_MISMATCH'
                WHEN pi.refund_amount > 0                             THEN 'REFUND'
                ELSE 'OK'
            END                             AS verdict
        FROM orders o
        LEFT JOIN kaspi_payout_items pi ON pi.order_id = o.id
        LEFT JOIN kaspi_payouts      p  ON p.id  = pi.payout_id
        LEFT JOIN operations         op ON op.id = p.operation_id
        WHERE o.source IN ('KASPI_SHOP', 'KASPI_PAY');
        """
    )
