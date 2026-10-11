"""Backfill order_materials consumptions into the stock ledger (P2-INV-002).

Revision ID: 0022_backfill_order_materials
Revises: 0021_stock_ledger
Create Date: 2026-10-04

Data migration. Every pre-ledger order_materials row becomes one
PRODUCTION_CONSUMPTION movement with ref_type='ORDER_MATERIALS',
ref_id=order_materials.id.

Ordering (spec §7): legacy history was written without the negative-stock
gate, so it may contain negative excursions. The integrity trigger is
therefore DROPPED before the INSERT and re-CREATED after it — all inside
this one migration transaction. History is taken as-given; the gate
applies only to new movements. v_material_stock will show honestly
negative on-hand for legacy data.

The INSERT is idempotent (WHERE NOT EXISTS on ref_type/ref_id), so the
migration is re-runnable.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_backfill_order_materials"
down_revision = "0021_stock_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the gate: legacy consumptions may have driven stock negative.
    op.execute("DROP TRIGGER IF EXISTS trg_stock_movements_integrity ON stock_movements;")

    # 2. Backfill. Idempotent via the partial unique index + NOT EXISTS.
    op.execute(
        """
        INSERT INTO stock_movements
            (ip_id, material_id, order_id, movement_date, quantity_delta,
             movement_type, ref_type, ref_id, reason, created_at)
        SELECT o.ip_id,
               om.material_id,
               om.order_id,
               om.used_at::date,
               -om.quantity,
               'PRODUCTION_CONSUMPTION',
               'ORDER_MATERIALS',
               om.id,
               'backfill: pre-ledger consumption (migration 0022)',
               om.created_at
        FROM order_materials om
        JOIN orders o ON o.id = om.order_id
        WHERE NOT EXISTS (
            SELECT 1 FROM stock_movements sm
            WHERE sm.ref_type = 'ORDER_MATERIALS' AND sm.ref_id = om.id
        );
        """
    )

    # 3. Re-create the gate for all future movements.
    op.execute(
        """
        CREATE TRIGGER trg_stock_movements_integrity
            BEFORE INSERT ON stock_movements
            FOR EACH ROW EXECUTE FUNCTION fn_stock_movements_integrity();
        """
    )

    # 4. Verification: every order_materials row must have a movement.
    result = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT count(*) FROM order_materials om
            WHERE NOT EXISTS (
                SELECT 1 FROM stock_movements sm
                WHERE sm.ref_type = 'ORDER_MATERIALS' AND sm.ref_id = om.id
            );
            """
            )
        )
        .scalar_one()
    )
    if result:
        raise RuntimeError(
            f"backfill incomplete: {result} order_materials rows have no "
            "stock_movements counterpart"
        )


def downgrade() -> None:
    # Remove only the backfilled rows; keep the trigger (structural 0021).
    # The immutability trigger's GUC escape is used deliberately here:
    # alembic runs as erp_migrator, which the STRONG pattern admits.
    op.execute("SET app.allow_financial_delete = 'on';")
    op.execute(
        """
        DELETE FROM stock_movements
        WHERE ref_type = 'ORDER_MATERIALS'
          AND reason = 'backfill: pre-ledger consumption (migration 0022)';
        """
    )
