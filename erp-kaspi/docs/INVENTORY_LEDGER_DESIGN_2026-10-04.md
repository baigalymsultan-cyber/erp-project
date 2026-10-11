# Stock Movement Ledger — Design

**Date:** 2026-10-04
**Status:** DESIGN ONLY — no code. Implements P2-INV-001 (stock ledger), P2-INV-003 (reservations), P2-INV-004 (WIP).
**Prerequisite:** P2-INV-002 DONE (`POST /orders/{order_id}/materials` committed at HEAD `cabfebb`).
**Source:** `docs/GAP_P2_PRODUCTION_2026-10-04.md` findings P2-INV-001/003/004/008.

## Design principles

1. **Quantity ledger ≠ financial valuation.** The ledger stores signed quantity deltas only — no `unit_cost`, no money. Financial valuation (FIFO / weighted average / last price) is a *separate derivation layer* that reads the ledger. This keeps the ledger correct regardless of which valuation method the owner later chooses, and prevents valuation bugs from corrupting physical truth.
2. **Append-only, like `operations`.** INSERT-only with an immutability trigger (same pattern as `fn_forbid_delete()` / `fn_settlement_links_immutable()` — §95, DI-005). Corrections are new compensating rows, never UPDATE/DELETE. Test purge escapes via `SET LOCAL app.allow_financial_delete = 'on'`, identical to existing guards.
3. **`materials` is a global catalog** (`sku` UNIQUE, no `ip_id`). The ledger is IP-scoped (`ip_id` on every row) because stock physically belongs to an ИП. When `order_id` is present, a DI-001-style integrity trigger enforces `movement.ip_id = order.ip_id`.
4. **Single source for quantity truth.** `v_material_stock` / `v_material_availability` derive on-hand from the ledger. No `quantity_on_hand` column anywhere — stored aggregates drift.

---

## 1. `stock_movements` table

```sql
CREATE TABLE IF NOT EXISTS stock_movements (
    id              BIGSERIAL       PRIMARY KEY,
    ip_id           BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    material_id     BIGINT          NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
    order_id        BIGINT          NULL     REFERENCES orders (id) ON DELETE CASCADE,
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
        'RECEIPT',        -- stock IN: purchase received, return from order, production output
        'CONSUMPTION',    -- stock OUT: recorded via order_materials (P2-INV-002)
        'ADJUSTMENT_IN',  -- count correction upward
        'ADJUSTMENT_OUT',  -- count correction downward
        'SCRAP',          -- waste / offcuts (always negative delta)
        'RETURN_SUPPLIER' -- returned to supplier (negative delta)
    )),
    CONSTRAINT chk_stock_movements_scrap_negative CHECK (
        movement_type <> 'SCRAP' OR quantity_delta < 0
    ),
    CONSTRAINT chk_stock_movements_receipt_positive CHECK (
        movement_type <> 'RECEIPT' OR quantity_delta > 0
    ),
    CONSTRAINT chk_stock_movements_reason_required CHECK (
        movement_type NOT IN ('ADJUSTMENT_IN', 'ADJUSTMENT_OUT', 'SCRAP')
        OR (reason IS NOT NULL AND length(btrim(reason)) > 0)
    )
);

CREATE INDEX IF NOT EXISTS ix_stock_movements_material ON stock_movements (material_id);
CREATE INDEX IF NOT EXISTS ix_stock_movements_ip_material ON stock_movements (ip_id, material_id);
CREATE INDEX IF NOT EXISTS ix_stock_movements_order ON stock_movements (order_id);
```

**Why signed `quantity_delta` instead of direction IN/OUT:** `SUM(quantity_delta)` is the on-hand — one column, one view, no FILTER clauses to get wrong. The gap report proposed `qty + direction CHAR(3)`; signed delta is strictly simpler and less error-prone.

**Why no `unit_cost`:** principle 1. Frozen purchase price already lives on `order_materials.price_at_time` (§38). Valuation layers are derived, not stored (see §5).

**Quantity integrity:**
- `fn_stock_movements_integrity()` BEFORE INSERT:
  - DI-001-style: if `order_id` present, `NEW.ip_id` must equal `orders.ip_id` (cross-IP consumption blocked).
  - Negative-stock policy: default **BLOCK** (safe). `SELECT SUM(quantity_delta)` for `(ip_id, material_id)` + `NEW.quantity_delta >= 0` else `RAISE EXCEPTION ... USING ERRCODE='check_violation'`. The owner decision "allow negative stock?" flips this gate to warn-only — the check is written so the policy is one branch, not a schema change.
- `fn_stock_movements_immutable()` BEFORE UPDATE OR DELETE — mirrors `fn_settlement_links_immutable()` exactly (DI-005 pattern), incl. the `app.allow_financial_delete` test-purge escape.
- Purge order (conftest `_BUSINESS_TABLES_PURGE_ORDER`): insert `"stock_movements"` before `"operations"` (same slot logic as `settlement_links` — it references `orders` and `materials`, both purged later).

**Derived views:**
```sql
-- On-hand per (IP, material)
CREATE OR REPLACE VIEW v_material_stock AS
SELECT ip_id, material_id,
       SUM(quantity_delta)::NUMERIC(15,3) AS on_hand
FROM stock_movements
GROUP BY ip_id, material_id;

-- Availability = on-hand minus active reservations
CREATE OR REPLACE VIEW v_material_availability AS
SELECT s.ip_id, s.material_id, s.on_hand,
       COALESCE(r.reserved, 0)::NUMERIC(15,3) AS reserved_qty,
       (s.on_hand - COALESCE(r.reserved, 0))::NUMERIC(15,3) AS available
FROM v_material_stock s
LEFT JOIN (
    SELECT ip_id, material_id, SUM(quantity)::NUMERIC(15,3) AS reserved
    FROM material_reservations
    WHERE status = 'ACTIVE' AND (expires_at IS NULL OR expires_at > now())
    GROUP BY ip_id, material_id
) r USING (ip_id, material_id);
```

---

## 2. Connection to existing tables

### 2.1 `materials` (catalog, unchanged)
Global catalog, `sku` UNIQUE, `current_price` (frozen into `order_materials.price_at_time` at consumption). No changes. The ledger references it via `material_id`; IP scoping lives on the ledger row, not the catalog.

### 2.2 `order_materials` (P2-INV-002 writer exists)
Every `order_materials` row created via `POST /orders/{order_id}/materials` must create a matching `stock_movements` row in the **same transaction**:
- `movement_type='CONSUMPTION'`, `quantity_delta = -quantity`, `order_id = order.id`, `ip_id = order.ip_id`,
  `ref_type='ORDER_MATERIALS'`, `ref_id = order_materials.id`.
- Implementation: inside `add_order_material()` service, after `session.flush()`. The negative-stock gate (§1) fires here — consumption beyond availability is rejected by default.
- **Backfill:** one-time data migration creating CONSUMPTION rows for pre-existing `order_materials` rows (ledger must start from a complete history, otherwise on-hand is wrong from day one). Rows pre-dating the ledger get `movement_date = used_at`.

### 2.3 `EXPENSE_MATERIAL` operations — P2-INV-008 (parallel universe)
Financial operations have **no `material_id`, no quantity** — an `EXPENSE_MATERIAL` op cannot mechanically become a RECEIPT row. This is why auto-posting is impossible without a schema/flow decision. Three owner options, unchanged from the gap report:

| Option | Flow | Ledger impact |
|--------|------|---------------|
| A. Auto IN on purchase | Add `material_id` + `quantity` to operations (or link table `operation_material_receipts`) | POSTED op auto-creates RECEIPT |
| B. Separate receipt step | Purchase op (financial) + explicit stock RECEIPT (physical) as two API calls | Manual, reconciled by date/counterparty |
| C. Direct-to-order allowed | Some purchases bypass stock entirely | RECEIPT rows only where applicable |

**What can be built now without the owner:** the RECEIPT movement type, the ledger, and a `POST /stock/receipts` endpoint (material_id, quantity, movement_date, ref to operation optional). Whichever flow the owner picks later plugs into the same ledger. **Do not** auto-create receipts from operations — that would invent the business semantics the owner must decide.

---

## 3. Reservation mechanics (P2-INV-003)

Reservations are **soft** — they reduce *availability* but never *on-hand*. Separate table (not ledger rows), so the ledger stays a record of physical truth:

```sql
CREATE TABLE IF NOT EXISTS material_reservations (
    id           BIGSERIAL       PRIMARY KEY,
    ip_id        BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    order_id     BIGINT          NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    material_id  BIGINT          NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
    quantity     NUMERIC(15,3)   NOT NULL,
    status       VARCHAR(20)     NOT NULL DEFAULT 'ACTIVE',
    reserved_at  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at   TIMESTAMPTZ     NULL,
    released_at  TIMESTAMPTZ     NULL,
    released_reason VARCHAR(50)  NULL,
    created_by   BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT chk_reservations_qty CHECK (quantity > 0),
    CONSTRAINT chk_reservations_status CHECK (status IN ('ACTIVE','RELEASED','CONSUMED'))
);
```

**Lifecycle:**
- `RESERVE`: INSERT ACTIVE row. Check `quantity <= v_material_availability.available` (visibility-first: warn, don't hard-block, until owner confirms hard-reservation policy — gap report's "visibility-only first").
- `RELEASE`: `status='RELEASED'`, `released_at=now()`, `released_reason` ∈ (`ORDER_CANCELLED`, `MANUAL`, `EXPIRED`, `SUPERSEDED`). Order CANCELLED → release all ACTIVE reservations for that order (hook in order status change service, not a trigger — business flow stays in the service layer).
- `CONSUME`: when `add_order_material()` runs for a reserved material, mark matching reservations CONSUMED (FIFO by `reserved_at`) and skip the availability re-check (already reserved).
- **Expiry:** `expires_at` checked at read time (`v_material_availability` excludes expired) — no background job required for correctness. A periodic job may flip them to `EXPIRED`/`RELEASED` for reporting hygiene; optional.

**Why not ledger rows with a flag:** a reservation is an *intention*, not a physical movement. Putting intentions in the physical ledger corrupts `SUM(quantity_delta)` = on-hand. Separate table, joined only in the availability view.

---

## 4. WIP derivation (P2-INV-004)

No new table. WIP = costs sunk into orders that are in production but not yet completed:

```sql
CREATE OR REPLACE VIEW v_wip_by_order AS
SELECT o.id AS order_id, o.ip_id, o.external_order_id, o.status,
       COALESCE(m.material_cost, 0)::NUMERIC(15,2) AS wip_materials,
       COALESCE(w.labour_cost, 0)::NUMERIC(15,2)   AS wip_labour,
       (COALESCE(m.material_cost, 0) + COALESCE(w.labour_cost, 0))::NUMERIC(15,2) AS wip_total
FROM orders o
LEFT JOIN (
    SELECT order_id, SUM(total_cost) AS material_cost
    FROM order_materials GROUP BY order_id
) m ON m.order_id = o.id
LEFT JOIN (
    SELECT order_id, SUM(total_sum) AS labour_cost
    FROM work_entries
    WHERE status IN ('CONFIRMED', 'PAID')   -- DRAFT excluded, mirrors order_direct_cost()
    GROUP BY order_id
) w ON w.order_id = o.id
WHERE o.status IN ('APPROVED', 'ASSEMBLING', 'DELIVERING');
```

**Semantics:** `NEW` (not started), `COMPLETED` (done), `CANCELLED`/`RETURNED` (abandoned) are excluded. `DELIVERING` included — goods left the shop but revenue may not be recognized yet (conservative). Material cost uses frozen `price_at_time` (§38), so WIP is historically correct. Expose via `GET /reports/wip` reusing the existing `/reports/order-costing` IP-isolation pattern.

---

## 5. Owner-blocked vs implementable now

| Item | Status | Owner question |
|------|--------|----------------|
| `stock_movements` ledger + immutability + same-IP + negative-stock gate | **Implementable now** | — |
| `v_material_stock`, `v_material_availability` | **Implementable now** | — |
| CONSUMPTION auto-row in `add_order_material()` + backfill migration | **Implementable now** | — |
| `material_reservations` (visibility-first, warn on over-reserve) | **Implementable now** | — |
| `v_wip_by_order` + `GET /reports/wip` | **Implementable now** | — |
| `POST /stock/receipts` (manual receipt) | **Implementable now** | — |
| Hard-block vs warn on over-reservation | OWNER | Reservation strictness |
| Negative stock: block (default) vs allow-with-warning | OWNER | Backorder policy |
| Inventory valuation: FIFO / weighted average / last price | OWNER | **Financial impact** — changes COGS and balance |
| Purchase→stock flow (auto / separate receipt / direct-to-order) | OWNER | P2-INV-008 — cannot be inferred from operations |
| Finished goods tracking (`product_id` on ledger) | OWNER | Make-to-stock vs make-to-order |
| Cost-layer derivation (valuation engine reading the ledger) | OWNER | Depends on valuation method |

**Explicitly out of scope for this design:** choosing FIFO/average/last-price, auto-posting receipts from operations, finished-goods `product_id` support, BOM (`product_bom`).

---

## 6. Test plan outline

**Ledger integrity (TDD, real PostgreSQL):**
1. RECEIPT increases on-hand; CONSUMPTION decreases; `v_material_stock` = Σ delta.
2. UPDATE/DELETE on `stock_movements` rejected (`check_violation`, DI-005 pattern).
3. Cross-IP consumption blocked: order of IP-A + movement `ip_id`=IP-B → `check_violation` (DI-001 pattern).
4. Negative-stock gate: consuming more than on-hand → rejected by default.
5. `quantity_delta = 0` rejected; SCRAP with positive delta rejected; ADJUSTMENT/SCRAP without `reason` rejected.
6. Backfill migration: existing `order_materials` rows produce exactly one CONSUMPTION row each (idempotent re-run safe via `ref_type`/`ref_id` uniqueness).

**Service/API:**
7. `POST /orders/{id}/materials` creates the CONSUMPTION movement atomically (same transaction — failure rolls back both).
8. `POST /stock/receipts` creates RECEIPT; IP isolation enforced.
9. Reservation: reserve → `available` decreases, `on_hand` unchanged; release on order cancel; consume marks CONSUMED; expired reservations excluded from availability.
10. WIP view: ASSEMBLING order shows partial materials+labour; COMPLETED/CANCELLED excluded; DRAFT work entries excluded.

**Regression:**
11. Full pytest, Ruff, Alembic single-head check, `test_downgrade_function_list_matches_database` (add new `fn_*` to baseline `_FUNCTIONS` — same fix as `fbbb160`).

---

## Migration sketch (for implementer)

1. `00NN_stock_movements.py`: table + indexes + `fn_stock_movements_integrity()` + `fn_stock_movements_immutable()` + triggers + views.
2. `00NN+1_material_reservations.py`: reservations table + availability view (or fold into 1).
3. `00NN+2_backfill_consumption.py`: data migration — CONSUMPTION rows for existing `order_materials` (guarded by `NOT EXISTS` on `ref_type='ORDER_MATERIALS', ref_id`).
4. Update `database/schema.sql` (source of truth), conftest purge order (`stock_movements` + `material_reservations` before `operations`), Alembic baseline `_FUNCTIONS`.
5. Service change: `add_order_material()` writes the ledger row; API: `POST /stock/receipts`, `GET /reports/wip`, reservation endpoints.
