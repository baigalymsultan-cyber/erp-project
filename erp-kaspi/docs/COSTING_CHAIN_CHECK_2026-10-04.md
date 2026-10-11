# Production Costing Chain Check — 2026-10-04

Read-only verification of the chain:
**Order → Production → Material Consumption → Work Entries → Direct Cost → WIP → Finished Product → Sale → P&L.**
No valuation method chosen or implied; only what is derivable without one.

## 1. Chain-link inventory

| # | Link | Table / Service / Function | Provides | Lacks | Refs |
|---|------|---------------------------|----------|-------|------|
| 1 | Order | `orders` table; `ORDER_STATUSES` 7 statuses | Order header: `total_amount` (selling revenue), `kaspi_commission`, `delivery_fee`, `status`, `completed_at`; `order_status_history` audit via trigger | No financial posting on status change (by design, §24) | `app/models/orders.py:60-148`, `database/schema.sql` §orders |
| 2 | Production | Order `status` workflow NEW→APPROVED→ASSEMBLING→DELIVERING→COMPLETED | Status tracking only | No production-order entity distinct from sales order; no routing/operations plan | `app/models/orders.py:59-68` |
| 3 | Material consumption | `order_materials` + `add_order_material()` + `POST /orders/{id}/materials` (P2-INV-002) | Qty consumed per order; `price_at_time` FROZEN from `materials.current_price` by trigger (§38); `total_cost` GENERATED `round(qty*price,2)` | No link to the `EXPENSE_MATERIAL` financial operation that paid for the material (P2-INV-008) | `app/services/__init__.py:182-211`, `app/models/orders.py:280-343` |
| 4 | Work entries (piecework) | `work_entries` + `add_work_entry()` | `rate_at_time` frozen by trigger (§32); `total_sum` GENERATED `qty*rate` (§33); statuses DRAFT/CONFIRMED/PAID; idempotency via fingerprint | `order_id` nullable (non-order work allowed); no confirmation service found in services (status set at creation/payroll) | `app/models/hr.py:254-330`, `app/services/__init__.py:100-148` |
| 5 | Direct cost | `v_order_costing` → `GET /reports/order-costing` (LIVE); `order_direct_cost()` service (DEAD CODE, 0 callers) | Per order: material_cost + labor_cost = total_direct_cost; gross_profit = total_amount − direct; margin % | See §2 divergences | `database/schema.sql:2460-2501`, `app/api/__init__.py:1128`, `app/services/__init__.py:155-180` |
| 6 | WIP | Nothing materialised | Derivable — see §3 (validated SQL, frozen values only) | No `v_wip_by_order` view; no WIP financial posting | — |
| 7 | Finished product | `products` catalogue only (sku/name/selling_price) | Sellable-item master data | No finished-goods stock, no production-output records, COMPLETED is a status flag only | `app/models/reference.py:236-260` |
| 8 | Sale → P&L | `operations` (SALE_KASPI_SHOP/PAY) → `pnl()` | Revenue recognised only via explicitly posted operations (manual `POST /operations` or ETL import); `KaspiPayout.operation_id` FK exists (nullable) | NO automatic order→SALE posting; NO automatic payout→operation posting (KASPI-002, external-blocker) | `app/models/kaspi.py:336-347`, `etl/import_excel.py:279-302` |

## 2. `order_direct_cost()` formula check (§37/§49)

Spec (§37, REQ-0308): **Direct Cost = Material + Piecework Labor**; overhead NOT allocated (OQ-010).

Service implementation (`app/services/__init__.py:155-180`):
- `material_cost = SUM(order_materials.total_cost)` — frozen `price_at_time` ✓ (§38/§49)
- `labor_cost = SUM(work_entries.total_sum)` — frozen `rate_at_time` ✓ (§32/§33)
- Formula matches spec. **BUT the function is dead code (0 callers)** — the live path is `v_order_costing`.

**Divergence A — DRAFT work entries (code/view drift):**
- Service sums **all** work_entries for the order (no status filter).
- View `v_order_costing` filters `work_entries.status IN ('CONFIRMED','PAID')` (`database/schema.sql:2493`).
- Live impact: none (service unused). Still a drift to fix or document.

**Divergence B — GAP-001 `direct_cost` vs order-level direct cost (REAL, structural):**
- `pnl()` GAP-001 `direct_cost` = SUM of **posted financial operations** typed `EXPENSE_MATERIAL` + `EXPENSE_KASPI_FEE` (ledger reality: money spent).
- `v_order_costing.material_cost` = SUM of **consumption** valued at frozen `price_at_time` (= `materials.current_price` at use time — effectively a last-price valuation, chosen implicitly by §38, not by owner).
- There is **no linkage** between the two: `EXPENSE_MATERIAL` operations carry no `material_id`/quantity (P2-INV-008). Purchase spend and consumption cost are two disconnected numbers that can diverge silently (buy in bulk in Jan, consume through Jun).
- Neither is "wrong" — but they answer different questions (cash spent vs. cost consumed), and nothing reconciles them.

## 3. WIP derivation (validated)

WIP needs **no valuation method**: both inputs are frozen historical values (`price_at_time`, `rate_at_time`), not a cost-flow assumption.

```sql
SELECT o.id AS order_id, o.status,
       COALESCE(m.mat_cost, 0)::NUMERIC(15,2) AS material_cost,
       COALESCE(w.lab_cost, 0)::NUMERIC(15,2) AS labour_cost,
       (COALESCE(m.mat_cost,0) + COALESCE(w.lab_cost,0))::NUMERIC(15,2) AS wip_value
FROM orders o
LEFT JOIN (SELECT order_id, SUM(total_cost) AS mat_cost
           FROM order_materials GROUP BY order_id) m ON m.order_id = o.id
LEFT JOIN (SELECT order_id, SUM(total_sum) AS lab_cost
           FROM work_entries
           WHERE status IN ('CONFIRMED','PAID') GROUP BY order_id) w ON w.order_id = o.id
WHERE o.status IN ('APPROVED','ASSEMBLING','DELIVERING');
```

Validated 2026-10-04 on `erp_test` (127.0.0.1:5432): runs clean, 0 rows (no production orders in test DB — expected).
Status filter mirrors `v_order_costing` (DRAFT labour excluded). COMPLETED/CANCELLED/RETURNED excluded: their cost is no longer WIP (COGS-or-scrap question needs the valuation owner decision only for the *financial* side; the quantity side is exact).

## 4. Finished goods: exists vs missing

| Exists | Missing |
|--------|---------|
| `products` catalogue: sku, name, selling_price, is_active (`app/models/reference.py:236`) | Finished-goods **stock** table (qty on hand per product/IP) |
| `order_items`: what was sold, historical `unit_price`, GENERATED total (§47) | **Production-output** records (order → qty of finished product produced) |
| `orders.status='COMPLETED'` + `completed_at` | Any link between COMPLETED and finished-goods receipt |
| | Make-to-stock flow (production without a sales order) |

A quantity-only `production_output` record (order_id, product_id, qty, produced_at, provenance) needs no valuation and is the missing link between WIP and finished goods.

## 5. Sale → P&L trace (exact path)

1. Order created (any source) → **no financial operation** (§24, enforced by design; `app/models/orders.py` docstring).
2. Order progresses through statuses → **no financial posting** at any transition (verified: no service touches operations on status change).
3. Revenue enters the ledger **only** via:
   - a. Manual `POST /operations` with `type_id` → SALE_KASPI_SHOP/PAY (`add_operation`, `app/services/__init__.py:617`);
   - b. ETL Excel import mapping a type column → operation type (`etl/import_excel.py:279-302`), idempotent via `import_fingerprint`;
   - c. (Designed, not built) payout → operation posting via `KaspiPayout.operation_id` FK (`app/models/kaspi.py:336-347`) — **KASPI-002 external-blocker**, no creation path exists.
4. `pnl()` aggregates POSTED operations with `accrual_date IS NOT NULL`, `kind='INCOME'` → `revenue`/`total_income` (accrual basis; GAP-001 layered since `8dd4b68`).
5. `v_order_costing.gross_profit = orders.total_amount − direct_cost` is **expected** margin on the selling price, **not recognised** P&L — it exists even if no SALE operation was ever posted. Do not confuse the two.

Commission path (per Kaspi official): sale → settlement (net of commission) → commission as separate `EXPENSE_KASPI_FEE` operation from the Kaspi Pay statement. No double-count as long as commission is never netted into the sale operation.

## 6. Gap list

### Implementable WITHOUT valuation (no owner needed)
- **G-C1**: `v_wip_by_order` view (§3 SQL) + API exposure — pure derivation, frozen values.
- **G-C2**: Quantity-only `production_output` records (order → finished product qty) — the missing WIP→finished-goods link.
- **G-C3**: `order_materials` ↔ `EXPENSE_MATERIAL` operations provenance linkage (P2-INV-008) — record which purchase funded which consumption; quantity/provenance only.
- **G-C4**: Fix/document Divergence A (`order_direct_cost` DRAFT handling vs `v_order_costing`) — dead code, but drift is a trap for the next reader.
- **G-C5**: Reconciliation report Divergence B (ledger spend vs. consumption cost) — a report can show both numbers side by side without choosing valuation.
- **G-C6**: `stock_movements` immutable quantity ledger (see `docs/INVENTORY_LEDGER_DESIGN_2026-10-04.md` — parallel agent's design).

### OWNER-blocked (genuinely financial semantics)
- **O-C1**: Inventory valuation method (FIFO / weighted average / last price). Note: §38's frozen `price_at_time` already embeds an implicit last-price choice for *consumption* — the owner should confirm or replace it explicitly.
- **O-C2**: OQ-010 overhead allocation (rent/utilities/ads → orders).
- **O-C3**: COGS recognition timing (on COMPLETED? on payout? on shipment?) — determines when WIP becomes expense.
- **O-C4**: AR/AP credit/refund semantics (BUG-1) — affects returns handling in the chain.
- **O-C5**: KASPI-002 payout→operation auto-posting (external-blocker + business rule).

## Verdict
The chain is **derivable end-to-end at quantity/frozen-cost level** without any valuation decision: consumption, labour, direct cost per order, and WIP are all exact. What is missing is linkage and materialisation (G-C1..G-C6), not semantics. The financial-statement side (COGS, inventory value on balance sheet) is what genuinely waits on O-C1/O-C3.
