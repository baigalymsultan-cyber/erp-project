"""Stock ledger foundation (P2-INV-001..004): quantity-only inventory.

Revision ID: 0021_stock_ledger
Revises: 0020_bank_statements
Create Date: 2026-10-04

Structural migration (idempotent):

1. stock_movements — the immutable quantity ledger. on_hand per
   (IP, material) = SUM(quantity_delta); signed deltas, 7 movement types
   with sign/type/reason guards. No unit_cost, no warehouse_id
   (owner-blocked / no warehouse entity).
2. material_reservations — reservations are intentions, NOT ledger rows
   (a reservation row would corrupt the on-hand invariant).
3. fn_stock_movements_integrity() + trigger: per-material FOR UPDATE
   serialization, DI-001 (movement IP == order IP), negative-stock gate.
4. fn_stock_movements_immutable() + trigger: DI-005 STRONG pattern —
   the GUC escape requires erp_migrator membership (SEC-HIGH 2026-10-04,
   same guard as fn_settlement_links_immutable). Never the GUC-only
   weak pattern.
5. fn_reservations_ip() + trigger: DI-001 for reservations.
6. Views: v_material_stock, v_material_availability (expiry read-time),
   v_wip_by_order (frozen price_at_time + confirmed labour).

Fresh databases get the same DDL from database/schema.sql (SECTION 3c);
everything here is idempotent (IF NOT EXISTS / CREATE OR REPLACE) so
baseline + upgrade agree.

The 0022 backfill (order_materials → PRODUCTION_CONSUMPTION) is a
separate data migration with its own trigger drop/recreate ordering.
"""

from __future__ import annotations

from alembic import op

revision = "0021_stock_ledger"
down_revision = "0020_bank_statements"
branch_labels = None
depends_on = None


_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS stock_movements (
    id              BIGSERIAL       PRIMARY KEY,
    ip_id           BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    material_id     BIGINT          NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
    order_id        BIGINT          NULL     REFERENCES orders (id) ON DELETE CASCADE,
    counterparty_id BIGINT          NULL     REFERENCES counterparties (id) ON DELETE SET NULL,
    movement_date   DATE            NOT NULL,
    quantity_delta  NUMERIC(15,3)   NOT NULL,
    movement_type   VARCHAR(30)     NOT NULL,
    ref_type        VARCHAR(30)     NULL,
    ref_id          BIGINT          NULL,
    reason          TEXT            NULL,
    created_by      BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_stock_movements_qty_nonzero CHECK (quantity_delta <> 0),
    CONSTRAINT chk_stock_movements_type CHECK (movement_type IN (
        'RECEIPT',
        'ISSUE',
        'PRODUCTION_CONSUMPTION',
        'PRODUCTION_OUTPUT',
        'RETURN',
        'ADJUSTMENT',
        'SCRAP'
    )),
    CONSTRAINT chk_stock_movements_sign CHECK (
        (movement_type IN ('RECEIPT', 'PRODUCTION_OUTPUT') AND quantity_delta > 0)
        OR (movement_type IN ('ISSUE', 'PRODUCTION_CONSUMPTION', 'RETURN', 'SCRAP')
            AND quantity_delta < 0)
        OR (movement_type = 'ADJUSTMENT')
    ),
    CONSTRAINT chk_stock_movements_reason_required CHECK (
        movement_type NOT IN ('ADJUSTMENT', 'SCRAP')
        OR (reason IS NOT NULL AND length(btrim(reason)) > 0)
    ),
    CONSTRAINT chk_stock_movements_order_scope CHECK (
        (movement_type NOT IN ('PRODUCTION_CONSUMPTION', 'PRODUCTION_OUTPUT'))
        OR order_id IS NOT NULL
    ),
    CONSTRAINT chk_stock_movements_return_counterparty CHECK (
        movement_type <> 'RETURN' OR counterparty_id IS NOT NULL
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_movements_ref
    ON stock_movements (ref_type, ref_id)
    WHERE ref_type IS NOT NULL AND ref_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_stock_movements_ip_material
    ON stock_movements (ip_id, material_id, movement_date);
CREATE INDEX IF NOT EXISTS ix_stock_movements_order
    ON stock_movements (order_id) WHERE order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS material_reservations (
    id               BIGSERIAL       PRIMARY KEY,
    ip_id            BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    order_id         BIGINT          NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    material_id      BIGINT          NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
    quantity         NUMERIC(15,3)   NOT NULL,
    status           VARCHAR(20)     NOT NULL DEFAULT 'ACTIVE',
    reserved_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at       TIMESTAMPTZ     NULL,
    released_at      TIMESTAMPTZ     NULL,
    released_reason  VARCHAR(50)     NULL,
    created_by       BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT chk_reservations_qty CHECK (quantity > 0),
    CONSTRAINT chk_reservations_status CHECK (status IN ('ACTIVE', 'RELEASED', 'CONSUMED')),
    CONSTRAINT chk_reservations_release CHECK (
        (status = 'ACTIVE' AND released_at IS NULL)
        OR (status <> 'ACTIVE' AND released_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_reservations_ip_material
    ON material_reservations (ip_id, material_id) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS ix_reservations_order
    ON material_reservations (order_id) WHERE status = 'ACTIVE';
"""

_TRIGGERS_DDL = """
CREATE OR REPLACE FUNCTION fn_stock_movements_integrity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_order_ip   BIGINT;
    v_on_hand    NUMERIC(15,3);
BEGIN
    PERFORM 1 FROM materials WHERE id = NEW.material_id FOR UPDATE;

    IF NEW.order_id IS NOT NULL THEN
        SELECT ip_id INTO v_order_ip FROM orders WHERE id = NEW.order_id;
        IF v_order_ip IS DISTINCT FROM NEW.ip_id THEN
            RAISE EXCEPTION
                'stock_movements: cross-IP movement blocked (DI-001). '
                'movement ip=%, order % ip=%',
                NEW.ip_id, NEW.order_id, v_order_ip
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF NEW.quantity_delta < 0 THEN
        SELECT COALESCE(SUM(quantity_delta), 0) INTO v_on_hand
        FROM stock_movements
        WHERE ip_id = NEW.ip_id AND material_id = NEW.material_id;
        IF v_on_hand + NEW.quantity_delta < 0 THEN
            RAISE EXCEPTION
                'stock_movements: insufficient stock for material % (ip=%). '
                'on_hand=%, requested=%',
                NEW.material_id, NEW.ip_id, v_on_hand, NEW.quantity_delta
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_stock_movements_integrity ON stock_movements;
CREATE TRIGGER trg_stock_movements_integrity
    BEFORE INSERT ON stock_movements
    FOR EACH ROW EXECUTE FUNCTION fn_stock_movements_integrity();

CREATE OR REPLACE FUNCTION fn_stock_movements_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_hatch_role TEXT := 'erp_migrator';
BEGIN
    -- DI-005 STRONG: GUC escape gated on erp_migrator membership
    -- (SEC-HIGH 2026-10-04). Never the GUC-only weak pattern.
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
        'stock_movements: rows are immutable (DI-005 pattern). '
        'Post a compensating movement, do not mutate.'
        USING ERRCODE = 'check_violation';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_stock_movements_immutable ON stock_movements;
CREATE TRIGGER trg_stock_movements_immutable
    BEFORE UPDATE OR DELETE ON stock_movements
    FOR EACH ROW EXECUTE FUNCTION fn_stock_movements_immutable();

CREATE OR REPLACE FUNCTION fn_reservations_ip()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_order_ip BIGINT;
BEGIN
    SELECT ip_id INTO v_order_ip FROM orders WHERE id = NEW.order_id;
    IF v_order_ip IS DISTINCT FROM NEW.ip_id THEN
        RAISE EXCEPTION
            'material_reservations: cross-IP reservation blocked (DI-001). '
            'reservation ip=%, order % ip=%',
            NEW.ip_id, NEW.order_id, v_order_ip
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_reservations_ip ON material_reservations;
CREATE TRIGGER trg_reservations_ip
    BEFORE INSERT OR UPDATE OF ip_id, order_id ON material_reservations
    FOR EACH ROW EXECUTE FUNCTION fn_reservations_ip();
"""

_VIEWS_DDL = """
CREATE OR REPLACE VIEW v_material_stock AS
SELECT ip_id,
       material_id,
       SUM(quantity_delta)::NUMERIC(15,3) AS on_hand,
       COUNT(*)::BIGINT                   AS movement_count
FROM stock_movements
GROUP BY ip_id, material_id;

CREATE OR REPLACE VIEW v_material_availability AS
SELECT s.ip_id,
       s.material_id,
       s.on_hand,
       COALESCE(r.reserved_qty, 0)::NUMERIC(15,3) AS reserved_qty,
       (s.on_hand - COALESCE(r.reserved_qty, 0))::NUMERIC(15,3) AS available_qty
FROM v_material_stock s
LEFT JOIN (
    SELECT ip_id, material_id,
           SUM(quantity)::NUMERIC(15,3) AS reserved_qty
    FROM material_reservations
    WHERE status = 'ACTIVE'
      AND (expires_at IS NULL OR expires_at > now())
    GROUP BY ip_id, material_id
) r USING (ip_id, material_id);

CREATE OR REPLACE VIEW v_wip_by_order AS
SELECT o.id                 AS order_id,
       o.ip_id,
       o.external_order_id,
       o.status,
       COALESCE(m.material_cost, 0)::NUMERIC(15,2) AS wip_materials,
       COALESCE(w.labour_cost, 0)::NUMERIC(15,2)    AS wip_labour,
       (COALESCE(m.material_cost, 0)
        + COALESCE(w.labour_cost, 0))::NUMERIC(15,2) AS wip_total
FROM orders o
LEFT JOIN (
    SELECT order_id, SUM(total_cost) AS material_cost
    FROM order_materials
    GROUP BY order_id
) m ON m.order_id = o.id
LEFT JOIN (
    SELECT order_id, SUM(total_sum) AS labour_cost
    FROM work_entries
    WHERE status IN ('CONFIRMED', 'PAID')
    GROUP BY order_id
) w ON w.order_id = o.id
WHERE o.status IN ('APPROVED', 'ASSEMBLING', 'DELIVERING');
"""


def upgrade() -> None:
    op.execute(_TABLES_DDL)
    op.execute(_TRIGGERS_DDL)
    op.execute(_VIEWS_DDL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_wip_by_order;")
    op.execute("DROP VIEW IF EXISTS v_material_availability;")
    op.execute("DROP VIEW IF EXISTS v_material_stock;")
    op.execute("DROP TRIGGER IF EXISTS trg_reservations_ip ON material_reservations;")
    op.execute("DROP FUNCTION IF EXISTS fn_reservations_ip();")
    op.execute("DROP TRIGGER IF EXISTS trg_stock_movements_immutable ON stock_movements;")
    op.execute("DROP TRIGGER IF EXISTS trg_stock_movements_integrity ON stock_movements;")
    op.execute("DROP FUNCTION IF EXISTS fn_stock_movements_immutable();")
    op.execute("DROP FUNCTION IF EXISTS fn_stock_movements_integrity();")
    op.execute("DROP TABLE IF EXISTS material_reservations;")
    op.execute("DROP TABLE IF EXISTS stock_movements;")
