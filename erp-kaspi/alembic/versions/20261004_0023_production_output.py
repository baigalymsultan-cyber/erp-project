"""Production output records (G-C2): quantity-only finished-goods link.

Revision ID: 0023_production_output
Revises: 0022_backfill_order_materials
Create Date: 2026-10-04

Structural migration (idempotent):

1. production_output — quantity-only link between production and finished
   goods (the missing WIP -> finished-goods link, docs/COSTING_CHAIN_CHECK).
   One row per production event; order_id NULL = make-to-stock (production
   without a sales order).
2. fn_production_output_immutable() + trigger: DI-005 STRONG pattern —
   the GUC escape requires erp_migrator membership (SEC-HIGH 2026-10-04,
   same guard as fn_stock_movements_immutable / fn_settlement_links_immutable).
   Never the GUC-only weak pattern. Corrections are compensating rows.
3. fn_production_output_ip() + trigger: DI-001 — when order_id is set,
   output IP must equal the order's IP (same pattern as fn_reservations_ip).
4. View v_finished_goods_stock: on-hand per (ip_id, product_id) =
   SUM(production_output.quantity) - SUM(order_items.quantity) on
   COMPLETED orders (chk_orders_status). Quantity only — no valuation
   (O-C1 owner-blocked).

Fresh databases get the same DDL from database/schema.sql (SECTION 3d /
SECTION 8.x); everything here is idempotent (IF NOT EXISTS /
CREATE OR REPLACE) so baseline + upgrade agree.

NO unit_cost, NO valuation columns: O-C1 is owner-blocked. Quantity only.
"""

from __future__ import annotations

from alembic import op

revision = "0023_production_output"
down_revision = "0022_backfill_order_materials"
branch_labels = None
depends_on = None


_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS production_output (
    id              BIGSERIAL       PRIMARY KEY,
    ip_id           BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    order_id        BIGINT          NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id      BIGINT          NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    quantity        NUMERIC(15,3)   NOT NULL,
    produced_at     TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by      BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason          TEXT            NULL,

    CONSTRAINT chk_production_output_qty CHECK (quantity > 0)
);

CREATE INDEX IF NOT EXISTS ix_production_output_ip_product
    ON production_output (ip_id, product_id);
CREATE INDEX IF NOT EXISTS ix_production_output_order
    ON production_output (order_id)
    WHERE order_id IS NOT NULL;
"""

_TRIGGERS_DDL = """
-- DI-005 STRONG: GUC escape requires erp_migrator membership (SEC-HIGH).
CREATE OR REPLACE FUNCTION fn_production_output_immutable()
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
        'production_output: rows are immutable (DI-005 pattern). '
        'Post a compensating row instead of UPDATE/DELETE.'
        USING ERRCODE = 'restrict_violation';
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_production_output_immutable ON production_output;
CREATE TRIGGER trg_production_output_immutable
    BEFORE UPDATE OR DELETE ON production_output
    FOR EACH ROW EXECUTE FUNCTION fn_production_output_immutable();

-- DI-001: output IP must equal the order's IP when order_id is set.
-- NULL order_id (make-to-stock) skips the check.
CREATE OR REPLACE FUNCTION fn_production_output_ip()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_order_ip BIGINT;
BEGIN
    IF NEW.order_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT ip_id INTO v_order_ip FROM orders WHERE id = NEW.order_id;
    IF v_order_ip IS DISTINCT FROM NEW.ip_id THEN
        RAISE EXCEPTION
            'production_output: cross-IP output blocked (DI-001). '
            'output ip=%, order % ip=%',
            NEW.ip_id, NEW.order_id, v_order_ip
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_production_output_ip ON production_output;
CREATE TRIGGER trg_production_output_ip
    BEFORE INSERT OR UPDATE OF ip_id, order_id ON production_output
    FOR EACH ROW EXECUTE FUNCTION fn_production_output_ip();
"""

_VIEWS_DDL = """
-- Finished-goods on-hand per (IP, product): produced minus delivered.
-- Delivered = SUM(order_items.quantity) on COMPLETED orders
-- (chk_orders_status). order_items.product_id is NULLable — excluded.
-- Quantity only, no valuation (O-C1 blocked).
CREATE OR REPLACE VIEW v_finished_goods_stock AS
SELECT p.ip_id,
       p.product_id,
       p.produced_qty,
       COALESCE(d.delivered_qty, 0)::NUMERIC(15,3) AS delivered_qty,
       (p.produced_qty - COALESCE(d.delivered_qty, 0))::NUMERIC(15,3) AS on_hand
FROM (
    SELECT ip_id, product_id, SUM(quantity)::NUMERIC(15,3) AS produced_qty
    FROM production_output
    GROUP BY ip_id, product_id
) p
LEFT JOIN (
    SELECT o.ip_id, oi.product_id,
           SUM(oi.quantity)::NUMERIC(15,3) AS delivered_qty
    FROM order_items oi
    JOIN orders o ON o.id = oi.order_id
    WHERE o.status = 'COMPLETED'
      AND oi.product_id IS NOT NULL
    GROUP BY o.ip_id, oi.product_id
) d USING (ip_id, product_id);
"""


def upgrade() -> None:
    op.execute(_TABLES_DDL)
    op.execute(_TRIGGERS_DDL)
    op.execute(_VIEWS_DDL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_finished_goods_stock;")
    op.execute("DROP TRIGGER IF EXISTS trg_production_output_ip ON production_output;")
    op.execute("DROP FUNCTION IF EXISTS fn_production_output_ip();")
    op.execute("DROP TRIGGER IF EXISTS trg_production_output_immutable ON production_output;")
    op.execute("DROP FUNCTION IF EXISTS fn_production_output_immutable();")
    op.execute("DROP TABLE IF EXISTS production_output;")
