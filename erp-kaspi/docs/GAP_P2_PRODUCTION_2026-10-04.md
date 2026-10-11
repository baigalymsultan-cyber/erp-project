# P2 Production/Inventory Gap Search — Furniture MRP Chain

**Date:** 2026-10-04
**Scope:** Order → Production → Materials → Work → Cost → Finished Product → Sale → Payment → P&L
**Files:** `database/schema.sql`, `app/services/__init__.py`, `app/api/__init__.py`, `app/models/`, `frontend/`

## Chain Trace Results

| Step | Status | Evidence |
|------|--------|----------|
| Order created | ✅ | `POST /orders`, `orders` table with statuses |
| Materials catalog | ✅ | `materials` table, `GET /materials`, frontend catalog |
| Products catalog | ✅ | `products` table, `GET /products` |
| Material consumption recorded | ⚠️ | `order_materials` table EXISTS, but **no API writer** |
| Stock ledger (qty on hand) | ❌ | No table, no column, no service |
| Material reservations | ❌ | None |
| Production orders | ❌ | None (orders have ASSEMBLING status but no production entity) |
| WIP tracking | ❌ | None |
| Finished goods stock | ❌ | None |
| BOM (product→materials) | ❌ | None |
| Direct cost calculation | ✅ | `order_direct_cost()`, `v_order_costing`, `GET /reports/order-costing` |
| Piecework labour | ✅ | `work_entries`, `POST /payroll/work-entries`, linked via `order_id` |
| Scrap/waste/returns/adjustments | ❌ | None |
| Material purchase → stock IN | ❌ | `EXPENSE_MATERIAL` operations have no inventory impact |

---

## Findings

### P2-INV-001: No stock ledger — quantity on hand not tracked

**Severity:** HIGH
**File/Table:** `database/schema.sql` — `materials` (line 1072), `products` (line 1051)
**Evidence:**
- `materials` has: `sku, name, unit, current_price, supplier_id` — NO `quantity_on_hand` or stock column
- `products` has: `sku, name, selling_price` — NO stock column
- Grep for `stock|inventory|warehouse` in schema.sql: zero table matches
- `docs/BALANCE_IMPLEMENTATION_MAP_2026-10-04.md:85-94` already documents: "Inventory (materials) — MISSING SOURCE"

**Business impact:** ERP cannot answer "what materials are in stock?" or "do we have enough material for order X?". Material purchases (`EXPENSE_MATERIAL` operations) are pure financial entries with no inventory impact. The Management Balance cannot include Inventory (confirmed missing in balance map).

**Proposed fix:** Create `stock_movements` table (per balance map spec lines 98-101):
```sql
stock_movements(id, ip_id, material_id, movement_date DATE, qty NUMERIC(15,3),
                direction CHAR(3), -- 'IN' | 'OUT'
                unit_cost NUMERIC(15,2), -- frozen cost for valuation
                ref_type VARCHAR(20), -- 'PURCHASE' | 'ORDER_CONSUME' | 'ADJUSTMENT' | 'RETURN'
                ref_id BIGINT, created_by, created_at)
```
Plus `v_material_stock` view: `SUM(qty WHERE IN) - SUM(qty WHERE OUT)` per material.

**Tests required:**
- Purchase creates IN movement → stock increases
- Order consumption creates OUT movement → stock decreases
- Cannot consume more than available (or allow negative with warning — OWNER decision)
- Stock valuation (FIFO/average — OWNER decision)

**OWNER decision required:** YES
- Q: Should `EXPENSE_MATERIAL` operations auto-create stock IN movements, or is stock tracked separately?
- Q: Costing method for inventory valuation: FIFO, weighted average, or last price?
- Q: Allow negative stock (backorders) or block?

---

### P2-INV-002: No API writer for material consumption

**Severity:** HIGH
**File:** `app/api/__init__.py` — no POST endpoint for `order_materials`
**Evidence:**
- `order_materials` table exists (schema.sql:1330) with `quantity`, `price_at_time`, `total_cost`
- Grep `@app.post` in api: 18 endpoints — none for `/orders/{id}/materials` or similar
- Grep `order_materials` in `app/` (non-test): only `app/models/orders.py` (model) and `app/services/__init__.py:160-166` (read for costing)
- Material consumption can ONLY be recorded via direct SQL

**Business impact:** The costing chain is broken at the data entry point. `v_order_costing` and `/reports/order-costing` will show `material_cost=0` for all orders unless someone writes SQL directly. The foreman/brigadier has no way to record "used 5 sheets of plywood for order #123" through the system.

**Proposed fix:**
1. `POST /orders/{order_id}/materials` — record consumption:
   - Input: `material_id`, `quantity`
   - `price_at_time` = `materials.current_price` (frozen, per §38)
   - Validate: order exists, user has IP access, material is active
   - If stock ledger exists (P2-INV-001): create OUT movement, check availability
2. `GET /orders/{order_id}/materials` — list consumed materials
3. `DELETE /orders/{order_id}/materials/{id}` — correction (or reversal pattern)

**Tests required:**
- POST creates order_materials with frozen price
- Price change in catalog doesn't affect already-recorded consumption
- IP isolation (cannot add materials to another IP's order)
- Quantity > 0 validation

**OWNER decision required:** NO — technical implementation, no financial semantics change.

---

### P2-INV-003: No material reservations for orders

**Severity:** MEDIUM
**File/Table:** None — no reservation entity exists
**Evidence:** No `reservations`, `material_allocations`, or similar table in schema.sql. Orders have no "reserved materials" concept.

**Business impact:** Cannot answer "are materials reserved for upcoming orders?" Two orders might plan to use the same material with no visibility into conflicts. For a furniture business with limited sheet goods, this causes production delays.

**Proposed fix:** Add `material_reservations(id, ip_id, order_id, material_id, quantity, reserved_at, released_at, status)` OR add `reserved_qty` to stock view. Simpler: reservation = soft OUT movement with `ref_type='RESERVATION'`.

**Tests required:**
- Reserve → available stock decreases (but on-hand unchanged)
- Order cancelled → reservation released
- Reservation converted to consumption on use

**OWNER decision required:** MAYBE — depends on whether business wants hard reservations or just visibility. Can implement as visibility-only first (no blocking).

---

### P2-INV-004: No production orders / WIP tracking

**Severity:** MEDIUM
**File/Table:** `orders.status` includes `ASSEMBLING` but no production entity
**Evidence:**
- `orders` has statuses: `NEW, APPROVED, ASSEMBLING, DELIVERING, COMPLETED, CANCELLED, RETURNED`
- `fn_log_order_status_change()` only logs to `order_status_history` — no WIP, no stock movement, no financial posting
- No `production_orders`, `work_orders`, or `wip` table
- `docs/BALANCE_IMPLEMENTATION_MAP_2026-10-04.md:151`: "marks the order done but posts no stock movement and no operation"

**Business impact:** Cannot track work-in-progress value. An order in ASSEMBLING status with 50% materials consumed and 50% labour done has no representation of partial completion. Management Balance cannot include WIP.

**Proposed fix (minimal):** Add `orders.completion_percent` OR derive WIP from:
- `WIP_materials = SUM(order_materials.total_cost WHERE order.status IN ('APPROVED','ASSEMBLING'))`
- `WIP_labour = SUM(work_entries.total_sum WHERE order.status IN ('APPROVED','ASSEMBLING') AND work_entries.status IN ('CONFIRMED','PAID'))`

This is derivable without new tables — just a view `v_wip_by_order`.

**Tests required:**
- WIP view shows correct partial costs for ASSEMBLING orders
- COMPLETED orders excluded from WIP
- CANCELLED orders excluded

**OWNER decision required:** NO for derivable WIP view. YES if they want explicit % completion tracking or WIP financial postings.

---

### P2-INV-005: No finished goods stock

**Severity:** MEDIUM
**File/Table:** `products` has no stock; no `finished_goods` movements
**Evidence:**
- `products` table: catalog only (no quantity)
- Order COMPLETED → no stock IN for products
- No link between `order_items` (products sold) and product inventory

**Business impact:** For make-to-stock furniture (not just make-to-order), cannot track finished inventory. Cannot answer "how many chairs do we have ready to sell?"

**Proposed fix:** Extend `stock_movements` (P2-INV-001) to support `product_id` (nullable, XOR with `material_id`). Order COMPLETED → IN movement for products in `order_items`. Sale → OUT movement.

**Tests required:**
- Order completion creates finished goods IN
- Sale decrements finished goods
- Make-to-order vs make-to-stock distinction

**OWNER decision required:** YES — is the business make-to-order, make-to-stock, or both? Affects whether finished goods tracking is needed.

---

### P2-INV-006: No BOM (Bill of Materials)

**Severity:** LOW
**File/Table:** None — no `product_materials` or `bom` table
**Evidence:** Grep for `bom|bill.of.material` in schema.sql and models: zero matches. No link between `products` and `materials`.

**Business impact:** Cannot auto-calculate material requirements for an order ("this chair needs 2m² plywood + 0.5L varnish"). Each order's materials must be entered manually. No standard costing.

**Proposed fix:** Create `product_bom(id, product_id, material_id, quantity_per_unit, unit)` table. When order created with products, suggest/copy BOM to `order_materials` as planned (not yet consumed).

**Tests required:**
- BOM defines materials per product
- Order creation can auto-populate planned materials from BOM
- Planned vs actual consumption variance report

**OWNER decision required:** NO for table structure. MAYBE for auto-population behavior (should it be automatic or manual confirmation?).

---

### P2-INV-007: No scrap / waste / returns / adjustments

**Severity:** LOW
**File/Table:** None
**Evidence:** No `scrap`, `waste`, `inventory_adjustments` tables. `order_materials` only records consumption (positive qty).

**Business impact:** Material waste (offcuts, defects) is invisible. If 10m² plywood purchased but only 8m² recorded as consumed, the 2m² variance is unexplained. No inventory adjustments for counts.

**Proposed fix:** Extend `stock_movements.ref_type` with `'SCRAP'`, `'ADJUSTMENT'`, `'RETURN'`. Allow negative adjustments with reason field.

**Tests required:**
- Scrap records OUT with reason
- Adjustment (count correction) with audit trail
- Returns to supplier

**OWNER decision required:** NO — technical, audit trail is standard.

---

### P2-INV-008: Material purchases not linked to inventory

**Severity:** MEDIUM (design question)
**File:** `operations` with `EXPENSE_MATERIAL` type
**Evidence:**
- Material purchase = financial `operation` with type `EXPENSE_MATERIAL`
- No link to `materials` table (no `material_id` on operations)
- No stock IN movement created
- `docs/BALANCE_IMPLEMENTATION_MAP_2026-10-04.md:106`: "are material purchases always stocked first, or can EXPENSE_MATERIAL be direct-to-order (bypassing stock)?"

**Business impact:** Even if stock ledger exists (P2-INV-001), purchases won't populate it automatically. Two parallel universes: financial (operations) and physical (stock).

**Proposed fix:** Two options:
- **A:** Add `material_id` + `quantity` to operations (or a link table `operation_materials`), auto-create stock IN on POSTED
- **B:** Separate "goods receipt" flow: purchase operation (financial) + stock IN (physical) as two steps, reconciled

**Tests required:** Depends on chosen option.

**OWNER decision required:** YES — fundamental: does every material purchase go through stock, or can materials be expensed directly to orders?

---

## What's Working (Positive Findings)

### ✅ Direct cost calculation is solid
- `order_direct_cost()` in `app/services/__init__.py:154` — materials + piecework
- `v_order_costing` view — revenue, material_cost, labor_cost, gross_profit, margin_percent
- `GET /reports/order-costing` endpoint with IP filtering, pagination
- `price_at_time` frozen by trigger (§38, ADR-008) — historical cost integrity
- Labour only counts CONFIRMED/PAID work entries (not DRAFT)

### ✅ Work entries link to orders
- `work_entries.order_id` NULLABLE FK to orders
- `POST /payroll/work-entries` exists for foreman input
- Rate frozen at entry time (§32)

### ✅ Order lifecycle tracked
- Status history via trigger → `order_status_history`
- 7 statuses including ASSEMBLING, COMPLETED, RETURNED

---

## Can ERP Answer Key Questions?

| Question | Answerable? | How / Why Not |
|----------|-------------|---------------|
| What is the true cost of order X? | ⚠️ Partial | Direct cost (materials + labour) YES via `/reports/order-costing`. But materials must be entered via SQL (P2-INV-002). No overhead (OQ-010). |
| What materials are in stock? | ❌ No | No stock ledger (P2-INV-001) |
| Do we have enough for order Y? | ❌ No | No stock + no reservations |
| What's in WIP? | ⚠️ Derivable | Not implemented, but derivable from existing tables (P2-INV-004) |
| What's our finished goods inventory? | ❌ No | No finished goods tracking (P2-INV-005) |
| What materials does product Z need? | ❌ No | No BOM (P2-INV-006) |
| Where did material variance go? | ❌ No | No scrap/adjustment tracking (P2-INV-007) |

---

## Priority Implementation Order

1. **P2-INV-002** (HIGH, no owner needed): API writer for `order_materials` — unblocks data entry
2. **P2-INV-001** (HIGH, owner needed): Stock ledger — foundation for all inventory
3. **P2-INV-008** (MEDIUM, owner needed): Purchase↔stock linkage — decides data flow
4. **P2-INV-004** (MEDIUM, no owner needed): WIP view — derivable, quick win
5. **P2-INV-003** (MEDIUM): Reservations — after stock ledger
6. **P2-INV-005** (MEDIUM, owner needed): Finished goods — if make-to-stock
7. **P2-INV-006** (LOW): BOM — efficiency improvement
8. **P2-INV-007** (LOW): Scrap/adjustments — after stock ledger

## Owner Decisions Summary

| ID | Question | Options |
|----|----------|---------|
| P2-INV-001 | Inventory valuation method? | A: FIFO, B: Weighted average, C: Last price |
| P2-INV-001 | Allow negative stock? | A: Block, B: Allow with warning |
| P2-INV-008 | Purchase → stock flow? | A: Auto IN on purchase, B: Separate receipt step, C: Direct-to-order allowed |
| P2-INV-005 | Make-to-stock? | A: Yes (need FG tracking), B: No (make-to-order only), C: Hybrid |
