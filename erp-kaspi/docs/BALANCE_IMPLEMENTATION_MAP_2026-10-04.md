# Management Balance — Implementation Map (2026-10-04)

**Scope**: analysis only, no code changes. Manufacturing context (furniture production).
**HEAD verified**: `54253e2`. All table/column/view names checked against `database/schema.sql`.
**Companion**: `docs/PNL_BALANCE_MAPPING_2026-10-03.md` (Part B), `docs/BOOK_SYNTHESIS_2026-10-03.md`.
**Book**: «Excel для финансового директора» (Актион, 2015) — management-architecture source only; no KZ tax/accounting rules invented.

**Balance equation**: `Equity = Assets − Liabilities`
`Assets = Cash + AR + Inventory + WIP + Finished Goods + Fixed Assets`
`Liabilities = AP + Payroll Liabilities + Tax/Other Liabilities + Loans`

Status legend: **EXISTS** (queryable today) · **DERIVABLE** (exact SQL below, no new tables)
· **MISSING** (new source required — spec given, not created) · **OWNER_DECISION** (blocked on a business choice)

**Score: 6 of 11 items DERIVABLE/EXISTS today** (Cash, AR, AP, Payroll Liabilities, WIP, Equity-formula).
**5 MISSING** (Inventory, Finished Goods, Fixed Assets, Tax/Other, Loans).

---

## 1. Cash — DERIVABLE

**Source**: `operations` (no balance stored anywhere — `accounts` has NO balance column, schema.sql:1095).
Cash is a pure cash-basis derivation: every POSTED operation moves cash by `operation_date`.

**Formula** — reuse the §13 function; `ending_balance` with an epoch start = cash position:

```sql
SELECT ending_balance AS cash
FROM cash_flow(:ip_id, DATE '2000-01-01', :asof_date);
```

Why this is correct: `start_bal` = Σ(amount) over all POSTED ops before period start;
`ending = start + INCOME + TRANSFER_IN − EXPENSE − TRANSFER_OUT` (schema.sql:2247-2253).
Transfers net to zero across own accounts, so inter-account moves don't change total cash. §28 compliant.

**Caveats**:
- Per-account breakdown is PARTIAL: `operations.account_id` is NULL-able and ETL/API paths don't always populate it. Per-account cash = `Σ(amount) WHERE account_id = :a AND status='POSTED'` — accurate only where `account_id` is maintained.
- Kaspi economics: payouts arrive as INCOME operations linked from `kaspi_payouts` (schema.sql:2228); whether SALE_KASPI_* ops and payout ops ever double-count the same economic event is a Kaspi-flow question (adjacent to KASPI_OQ_003) — flag, don't assume.

---

## 2. AR (Receivables) — EXISTS

**Source**: `v_counterparty_balances` (schema.sql:2614, created by migration 0015).
`balance = Σ(amount WHERE accrual_date IS NOT NULL) − Σ(amount WHERE accrual_date IS NULL)`,
POSTED-only, per `(ip_id, counterparty_id)`.

```sql
-- AR: counterparties that owe us
SELECT ip_id, counterparty_id, counterparty_name,
       balance AS ar_amount
FROM v_counterparty_balances
WHERE ip_id = :ip_id AND balance > 0;
```

**Limits (Tier 1)**:
- No aging, no due dates, no invoice documents — `operations` has no `due_date`; Tier 2
  (`ar_invoices` + `ar_payment_allocations`) is designed but deferred → **OWNER_DECISION (TD-024)**:
  does the business need invoice-level tracking, or is counterparty-level enough?
- No `opening_balances` table yet (GAP-3B): opening AR/AP = 0 until implemented. Historical
  backfill (migration 0015) set `accrual_date = operation_date` for `affects_pnl` POSTED ops,
  so pre-0015 accruals ≈ cash basis — acceptable, documented, not silently reconstructed.
- Advance payments from customers appear as negative balances (correct economically: a liability),
  but the view doesn't label them as advances.

---

## 3. AP (Payables) — EXISTS

**Source**: same `v_counterparty_balances`; negative balance = we owe the counterparty.

```sql
-- AP: counterparties we owe
SELECT ip_id, counterparty_id, counterparty_name,
       -balance AS ap_amount
FROM v_counterparty_balances
WHERE ip_id = :ip_id AND balance < 0;
```

**Limits**: same as AR (no aging/due dates/invoices; opening = 0; Tier 2 → TD-024 owner decision).
Prepayments to suppliers appear as positive balances (correct: an asset/claim), unlabeled.

---

## 4. Inventory (materials) — MISSING SOURCE

**Current state**: `materials` (schema.sql:1072) is a **catalogue** — `sku, name, unit, current_price`,
no quantity column. `order_materials` records consumption only (quantity + frozen `price_at_time`).
`operations` records material *purchases* as amounts (EXPENSE_MATERIAL) but **no quantities**.

**Why not derivable**: stock-on-hand needs quantities in AND out. Purchases exist only as KZT
amounts; consumption exists as quantities. No common unit → cannot reconcile.

**Required** (spec only, not created): a stock ledger, e.g.

```sql
-- PROPOSED (not implemented):
-- stock_movements(id, ip_id, material_id, movement_date DATE, qty NUMERIC(15,3),
--                 direction CHECK IN ('IN','OUT'), source_type TEXT, source_id BIGINT,
--                 unit_cost NUMERIC(15,2))
-- Inventory value = Σ(qty*unit_cost WHERE IN) − Σ(qty*unit_cost WHERE OUT), per material.
```

**OWNER_DECISION**: valuation method for the balance (average cost vs FIFO). For furniture
production average cost is the common default — but the choice changes reported margins,
so it stays with the owner. Also: are material purchases always stocked first, or can
EXPENSE_MATERIAL be direct-to-order (bypassing stock)? Affects whether every purchase = stock IN.

---

## 5. WIP (Work in Progress) — DERIVABLE (with stated valuation rule)

**Sources**: `orders` (statuses NEW/APPROVED/**ASSEMBLING**/DELIVERING/COMPLETED/…, schema.sql:1252),
`order_materials` (materials consumed, price frozen at use, `total_cost` GENERATED),
`work_entries` (piecework labor, `total_sum` GENERATED, status DRAFT/CONFIRMED/PAID).

**Valuation rule used** (explicit; cutoff is an owner decision — see below):
- Open production orders = status IN ('APPROVED','ASSEMBLING','DELIVERING').
- Materials WIP = Σ(`order_materials.total_cost`) attached to open orders.
- Labor WIP = Σ(`work_entries.total_sum`) for open orders with entry status = 'CONFIRMED'.

```sql
-- WIP value per IP as of now
WITH open_orders AS (
    SELECT id FROM orders
    WHERE ip_id = :ip_id
      AND status IN ('APPROVED', 'ASSEMBLING', 'DELIVERING')
),
mat AS (
    SELECT COALESCE(SUM(om.total_cost), 0) AS c
    FROM order_materials om JOIN open_orders o ON o.id = om.order_id
),
lab AS (
    SELECT COALESCE(SUM(we.total_sum), 0) AS c
    FROM work_entries we JOIN open_orders o ON o.id = we.order_id
    WHERE we.status = 'CONFIRMED' AND we.ip_id = :ip_id
)
SELECT ((SELECT c FROM mat) + (SELECT c FROM lab))::NUMERIC(15,2) AS wip_value;
```

**OWNER_DECISION**: (a) WIP cut-off — do APPROVED (not yet started) orders count as WIP, or only
ASSEMBLING+? (b) labor recognition — CONFIRMED only, or DRAFT too? (c) overhead in WIP — no
(kept out per OQ-010; WIP = direct cost only, consistent with §37).

---

## 6. Finished Goods — MISSING SOURCE

**Current state**: `products` (schema.sql:1051) is a catalogue — `sku, name, selling_price`,
**no quantity**. There is no "production completed → FG received" event: `orders.status='COMPLETED'`
marks the order done but posts no stock movement and no operation.

**Required** (spec only): FG stock ledger, e.g. `fg_movements(product_id, movement_date, qty,
direction IN ('PRODUCED','SHIPPED'/'SOLD'), order_id)`.
FG value = Σ produced × unit cost − Σ shipped × unit cost; unit cost needs the WIP valuation
of the completed order (links WIP → FG, which is why FG comes after WIP in build order).

**OWNER_DECISION**: what is the FG receipt event (order COMPLETED? a separate acceptance?),
and FG valuation (actual order cost vs standard cost).

---

## 7. Fixed Assets — MISSING SOURCE

**Current state**: no table at all (verified: zero `fixed_asset`/`depreciat` DDL hits in schema.sql).

**Required** (spec only): `fixed_assets(id, ip_id, name, acquisition_date DATE,
acquisition_cost NUMERIC(15,2), useful_life_months INT, depreciation_method TEXT,
status)`. Net book value = cost − accumulated depreciation; depreciation posts as operations
(new type, e.g. `EXPENSE_DEPRECIATION`, `affects_pnl=TRUE`, `accrual_date` = period month,
no cash movement — `operation_date` = accrual date, amount negative).

**OWNER_DECISION**: depreciation method (straight-line is the default assumption — confirm),
useful lives policy, capitalization threshold (what counts as FA vs immediate expense).

---

## 8. Payroll Liabilities — DERIVABLE

**Sources**: `payroll_summary.total_calculated` (GENERATED: piecework+base+bonuses−deductions,
schema.sql:1698), `payroll_payments.amount` (status POSTED/REVERSED, schema.sql:1731).
DB guard `fn_payroll_payment_no_overpay()` (migration 0013) guarantees
Σ(payments) ≤ total_calculated — liability can't go negative through the app path.

```sql
-- Outstanding payroll liability per employee (as of now)
SELECT s.ip_id, s.employee_id,
       (s.total_calculated
        - COALESCE(SUM(p.amount) FILTER (WHERE p.status = 'POSTED'), 0)
       )::NUMERIC(15,2) AS payroll_liability
FROM payroll_summary s
LEFT JOIN payroll_payments p ON p.payroll_summary_id = s.id
WHERE s.ip_id = :ip_id
  AND s.status IN ('CALCULATED', 'APPROVED')   -- PAID/CANCELLED owe nothing
GROUP BY s.ip_id, s.employee_id, s.total_calculated
HAVING (s.total_calculated
        - COALESCE(SUM(p.amount) FILTER (WHERE p.status = 'POSTED'), 0)) > 0;
```

**Notes**: liability is recognized at *calculation* (accrual basis — matches the book's
начисление/оплата split); `payroll_summary.status` is the lifecycle gate (TD-020 frozen —
no changes here). Payments with `operation_id IS NULL` (cash without ledger op) still reduce
the liability via `payroll_payments` — correct, no double count since `cash_flow()` only sums
`operations`.

---

## 9. Tax / Other Liabilities — MISSING SOURCE (+ OWNER_DECISION)

**Current state**: `tax_rates_history` (schema.sql:1119) stores **rates only** — no tax liability
postings, no tax operations, no tax payable tracking. Comment in schema: "Actual values are NOT
seeded — the tax regime is an open question (OQ-001)".

**Required** (spec only): post tax accruals as operations (or a `tax_liabilities` ledger) once the
regime is known. Nothing to derive until then.

**OWNER_DECISION (OQ-001)**: which tax regime applies (IP tax modes in KZ), what is taxable,
period, and whether tax posts to the ledger automatically or manually. No invention — official
KZ sources only.

---

## 10. Loans — MISSING SOURCE

**Current state**: no table (verified: zero `loan` DDL hits).

**Required** (spec only): `loans(id, ip_id, counterparty_id lender, principal NUMERIC(15,2),
received_date DATE, interest_rate NUMERIC, maturity_date DATE, status)` +
`loan_payments(loan_id, payment_date, principal_part, interest_part)`.
Outstanding = principal − Σ(principal_part); interest accrues to P&L financing section
(which is also missing — see P&L mapping gap #4).

**OWNER_DECISION**: does the business need loan tracking in ERP now, or is this future scope?
(Not in FINANCE-OPS-001 — requirement candidate, not a defect.)

---

## 11. Equity — DERIVABLE (formula; blocked on components)

**Source**: none needed — book pattern is derivation: `Equity = Assets − Liabilities`.

```sql
-- Once components exist:
-- equity = (cash + ar + inventory + wip + fg + fa_net)
--        - (ap + payroll_liab + tax_liab + loans)
```

**Status**: formula ready; meaningful only after the MISSING items above are implemented.
No separate equity table needed initially (opening equity enters via `opening_balances`,
GAP-3B, when that table is built). Inter-IP transfers within consolidation need elimination
rules — flagged, not designed here.

---

## Build order (unchanged from 2026-10-03 analysis)

1. **Cash** (derivable now) → 2. **AR/AP** (exists now) → 3. **Payroll liabilities** (derivable now)
   → 4. **Inventory** stock ledger (new) → 5. **WIP** (derivable now; FG after) →
   6. **Fixed assets + depreciation** (new) → 7. **Loans / taxes** (new + OQ-001) →
   8. **Equity** (derived) → 9. **Opening balances** table (GAP-3B, feeds AR/AP + Equity)

Steps 1–3 and 5 can be exposed as read-only balance views/APIs today with zero new tables.

## Owner decisions consolidated (exact questions)

| # | Item | Exact question for owner |
|---|---|---|
| 1 | AR/AP Tier 2 (TD-024) | Counterparty-level balances enough, or invoice-level (due dates, aging, allocation policy FIFO/manual)? |
| 2 | WIP cut-off | Which order statuses count as WIP — APPROVED included or ASSEMBLING+ only? CONFIRMED labor only? |
| 3 | Inventory valuation | Average cost or FIFO for material stock on the balance? |
| 4 | FG receipt | What event creates FG stock — order COMPLETED or separate acceptance? Actual vs standard cost? |
| 5 | Fixed assets | Straight-line depreciation? Useful lives? Capitalization threshold? |
| 6 | Tax regime (OQ-001) | Which KZ IP tax regime applies; what/when to accrue? (official sources only) |
| 7 | Loans | In scope now, or future? |
| 8 | Opening balances (GAP-3B) | Provide opening AR/AP/cash/equity figures as of which date? |
