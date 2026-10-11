# AR/AP Model Analysis: Derived vs Dedicated (2026-10-03)

**Task**: Determine whether existing `operations` + relations suffice for AR/AP, or a specialized model is needed.
**Scope**: Analysis only. No code changes. No invented semantics.
**Related**: TD-024, `docs/BOOK_ANALYSIS_ARAP_TREASURY_2026-10-03.md` (GAP-6.1/6.2, GAP-7.1), Phase 1 (accrual_date — NOT yet implemented, verified 2026-10-03).

---

## 1. Current schema inventory (verified)

### 1.1 `operations` table (schema.sql:1490-1528)

| Column | AR/AP relevance |
|---|---|
| `id` | PK |
| `ip_id` | ✅ IP dimension — EXISTS |
| `type_id` → `operation_types` | ⚠️ PARTIAL — kind is only INCOME/EXPENSE/TRANSFER; **no accrual vs payment distinction** |
| `category_id` | Expense/income category, not AR/AP |
| `counterparty_id` | ✅ Counterparty linkage — EXISTS (nullable FK) |
| `amount` | ✅ Original amount — EXISTS |
| `operation_date` | ⚠️ Single date only — **no accrual_date, no due_date** |
| `external_id` | ⚠️ Source system ID — EXISTS but not a document number |
| `related_order_id` | ⚠️ Links to orders — EXISTS, not invoices |
| `related_operation_id` | ⚠️ **Reserved**: reversal/correction links + TRANSFER_OUT↔TRANSFER_IN pairing (schema.sql:1525, services/__init__.py:993-994). NOT available for payment↔accrual linkage without semantic overload |
| `raw_payload_id` | Kaspi payload link |
| `comment` | Free text |
| `account_id`, `status`, `import_batch_id`, `import_fingerprint` | Not AR/AP |

### 1.2 What's missing (verified by grep)

- **No `due_date`** anywhere in schema.sql or models
- **No `accrual_date`** anywhere (Phase 1 pending)
- **No `document_no` / `invoice_no`** anywhere
- **No receivable/payable/aging** tables, views, or code references (case-insensitive grep: zero hits)
- **No "remaining balance"** concept anywhere
- `counterparties` table: only id, name, bin_iin, phone, comment — no credit terms, no payment terms

### 1.3 `operation_types` seed data (schema.sql:2433-2441)

Types: SALE_KASPI_SHOP, SALE_KASPI_PAY, EXPENSE_MATERIAL, EXPENSE_KASPI_FEE, SALARY_PAYMENT, TRANSFER_IN, TRANSFER_OUT.
**None distinguish accrual (debt creation) from payment (debt settlement).** A SALE_KASPI_SHOP could be a cash sale or a credit sale — the type doesn't tell you.

---

## 2. Field-by-field mapping: required AR/AP fields

| # | Required field | Status | Evidence |
|---|---|---|---|
| 1 | original amount | ✅ EXISTS | `operations.amount` on the accrual operation |
| 2 | paid amount | ❌ NOT LINKABLE | No payment↔accrual linkage mechanism; `related_operation_id` is semantically reserved |
| 3 | remaining amount | ❌ DERIVABLE ONLY WITH #2 | `original − paid` requires the linkage from #2 |
| 4 | accrual date | ❌ MISSING | No `accrual_date` (Phase 1 pending) |
| 5 | due date | ❌ MISSING | No `due_date` anywhere in schema |
| 6 | payment date(s) | ⚠️ PARTIAL | `operation_date` exists on payment ops, but they're not linked to accruals |
| 7 | counterparty | ✅ EXISTS | `operations.counterparty_id` |
| 8 | IP | ✅ EXISTS | `operations.ip_id` |
| 9 | document/source | ⚠️ PARTIAL | `external_id`, `related_order_id` exist; no invoice/document number |
| 10 | aging | ❌ NEEDS #4 | `report_date − accrual_date` — blocked on missing accrual_date |
| 11 | status | ❌ MISSING | `operations.status` is POSTED/REVERSED/CORRECTED/DRAFT (accounting lifecycle), not paid/partial/overdue (AR/AP lifecycle) |

**Score**: 3/11 exist, 2/11 partial, 6/11 missing or blocked.

---

## 3. The derived approach: what works

### 3.1 Counterparty-level balance (сальдо взаиморасчетов)

The book's formula (Ch 1): `Balance = Opening + Accruals − Payments`

**SQL sketch** (works IF we add accrual/payment classification + accrual_date):

```sql
-- Requires: op.is_accrual flag (or operation_types.accrual_kind), operations.accrual_date
SELECT
    o.ip_id,
    o.counterparty_id,
    COALESCE(SUM(o.amount) FILTER (WHERE o.is_accrual AND o.amount > 0), 0)
      - COALESCE(SUM(o.amount) FILTER (WHERE NOT o.is_accrual AND o.amount > 0), 0)
      AS ar_balance,  -- positive = customer owes us
    COALESCE(SUM(-o.amount) FILTER (WHERE o.is_accrual AND o.amount < 0), 0)
      - COALESCE(SUM(-o.amount) FILTER (WHERE NOT o.is_accrual AND o.amount < 0), 0)
      AS ap_balance   -- positive = we owe supplier
FROM operations o
WHERE o.status = 'POSTED'
  AND o.counterparty_id IS NOT NULL
GROUP BY o.ip_id, o.counterparty_id;
```

**Verdict for counterparty balances**: ✅ DERIVABLE with minimal additions:
1. Accrual vs payment classification (new boolean on operations, or new kind on operation_types)
2. `accrual_date` (Phase 1)
3. Opening balances (one-time data input, per book p. 35: "внести только один раз")

This gives total AR/AP per counterparty. **No dedicated table needed for this level.**

### 3.2 Where the derived approach breaks

**Break #1: Payment-to-invoice matching (many-to-many)**

Scenario: Counterparty has 3 unpaid invoices ($100 Jan 1, $200 Feb 1, $150 Mar 1). A $250 payment arrives. Which invoices are paid?

- The derived approach (group by counterparty) gives: total accrued $450, total paid $250, balance $200. ✅ Correct total.
- But it **cannot tell you** which invoices remain open. FIFO? LIFO? Manual allocation? The book requires per-invoice tracking (Ch 4: "единицей учета дебиторской задолженности является отдельная отгрузка").

**This is a mathematical impossibility, not a missing feature**: aggregation destroys the invoice-level information. No query over operations can recover which specific invoice a payment settled without an explicit allocation record.

**Break #2: Due dates are per-invoice**

A counterparty may have different payment terms per invoice (net-30 for invoice A, net-60 for invoice B). `due_date` cannot be derived from operations — it must be stored per invoice. Without it, overdue vs not-yet-due distinction is impossible.

**Break #3: Partial payments across periods**

If invoice A ($1000) receives $300 in Jan, $400 in Feb, $300 in Mar:
- Derived: balance decreases correctly. ✅
- But aging the remaining $300 in Feb requires knowing that $700 of invoice A is paid and $300 remains. The counterparty-level aggregate can't distinguish "invoice A partially paid" from "invoice A unpaid + invoice B fully paid."

**Break #4: Prepayments and overpayments**

Payment before accrual creates a negative balance. Mathematically fine, but business semantics differ:
- Prepayment (advance for future delivery) vs overpayment (error to refund) vs credit memo
- The derived approach cannot distinguish these without explicit modeling.

**Break #5: Status lifecycle**

AR/AP has its own lifecycle: OPEN → PARTIAL → PAID → OVERDUE → WRITTEN_OFF.
`operations.status` (POSTED/REVERSED/CORRECTED/DRAFT) tracks the accounting lifecycle, not the collection lifecycle. Overloading it would violate the "no second accounting architecture" constraint.

---

## 4. Verdict: two-tier approach

### Tier 1: Counterparty balances — DERIVED (no new table)

**Sufficient for**: Total AR/AP per counterparty, the book's "сальдо взаиморасчетов."

**Requires** (minimal additions to existing schema):
1. `operations.accrual_date` (Phase 1)
2. Accrual vs payment classification (new column or operation_types extension)
3. Opening balance input mechanism

**Proof of sufficiency**: The SQL sketch in §3.1 produces correct balances. No information is lost at the counterparty aggregation level.

### Tier 2: Invoice-level AR/AP with aging — DEDICATED MODEL REQUIRED

**Required for**: Per-invoice tracking, aging buckets, due dates, payment allocation, overdue detection, the book's Ch 4 "Контроль дебиторской задолженности."

**Proof of necessity** (§3.2 Breaks #1-5):
- Many-to-many payment↔invoice matching is **mathematically impossible** to derive from aggregated operations
- Due dates are per-invoice attributes with no derivable source
- Status lifecycle is distinct from operations.status

**Minimal dedicated model** (not implemented, for design reference):

```sql
-- Invoice (AR document): one row per shipment/invoice
CREATE TABLE ar_invoices (
    id BIGSERIAL PRIMARY KEY,
    ip_id BIGINT NOT NULL REFERENCES ip_entities(id),
    counterparty_id BIGINT NOT NULL REFERENCES counterparties(id),
    document_no VARCHAR(100) NOT NULL,           -- invoice/shipment number
    document_date DATE NOT NULL,                 -- = accrual_date
    due_date DATE NOT NULL,                      -- payment terms
    total_amount NUMERIC(15,2) NOT NULL,          -- original amount
    accrual_operation_id BIGINT REFERENCES operations(id),  -- link to accrual op
    status VARCHAR(20) NOT NULL,                 -- OPEN, PARTIAL, PAID, OVERDUE
    UNIQUE (ip_id, counterparty_id, document_no)
);

-- Payment allocation: many-to-many (one payment → multiple invoices)
CREATE TABLE ar_payment_allocations (
    id BIGSERIAL PRIMARY KEY,
    invoice_id BIGINT NOT NULL REFERENCES ar_invoices(id),
    payment_operation_id BIGINT NOT NULL REFERENCES operations(id),
    allocated_amount NUMERIC(15,2) NOT NULL,      -- portion of payment applied here
    UNIQUE (invoice_id, payment_operation_id)
);
-- Same pattern for AP (ap_invoices, ap_payment_allocations) or single table with direction
```

**Why not reuse `related_operation_id`**: It's semantically reserved for reversals and transfer pairing. Overloading it for payment↔invoice links would create ambiguity (is this operation a reversal of X or a payment for X?). The junction table is explicit and supports many-to-many.

---

## 5. Implementation dependencies

### For Tier 1 (derived balances):
1. Phase 1: `accrual_date` on operations
2. Accrual/payment classification (owner decision: new column vs operation_types.kind extension)
3. Opening balances (owner provides data)

### For Tier 2 (dedicated invoice model):
1. All Tier 1 dependencies
2. Owner decision: AR/AP model semantics (TD-024) — invoice vs counterparty-only
3. Owner decision: Payment allocation policy (FIFO default? manual?)
4. Owner decision: Aging bucket boundaries (GAP-7.1)

---

## 6. Recommendation

**Start with Tier 1** (derived counterparty balances). It's:
- Minimal schema change (accrual_date + classification flag)
- Mathematically sound (proven by SQL sketch)
- Sufficient for the book's "сальдо взаиморасчетов"
- Unblocks TD-024 at the counterparty level

**Defer Tier 2** (dedicated invoice model) until:
- Owner confirms invoice-level tracking is needed (vs counterparty-level)
- Payment allocation policy is decided
- There's actual business volume requiring per-invoice aging

**Do NOT**: Create the invoice tables preemptively. The constraint "do not create a new model without proof of necessity" is satisfied for Tier 2's *design* (proof in §3.2) but not for its *implementation* — implement only when the business need is confirmed.

---

## 7. Open owner decisions (narrowed to A/B/C)

**Q1: AR/AP granularity**
- A: Counterparty-level only (Tier 1 derived) — simplest, matches book's saldo formula
- B: Invoice-level (Tier 2 dedicated) — full Ch 4 compliance, requires allocation policy
- C: Both (Tier 1 now, Tier 2 later) — **recommended**

**Q2: Payment allocation policy** (if B or C)
- A: FIFO (oldest invoice first) — automatic, auditable
- B: Manual allocation by user — flexible, requires UI
- C: FIFO default with manual override — **recommended**

**Q3: Accrual/payment classification mechanism**
- A: New boolean `is_accrual` on operations — explicit per operation
- B: New `accrual_kind` on operation_types — type-level, less flexible
- C: Derive from (type, direction, counterparty presence) — **not recommended** (fragile, implicit)
