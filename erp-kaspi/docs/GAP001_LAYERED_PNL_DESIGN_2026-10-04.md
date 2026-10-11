# GAP-001: Layered P&L Design — What Can Be Built WITHOUT OQ-010

**Date:** 2026-10-04
**Status:** DESIGN ONLY — no code changed
**Constraint:** OQ-010 (overhead allocation rules) is OWNER BLOCKED. This design builds
every P&L layer that is definable from existing data, and explicitly marks what
stays owner-blocked. No accounting semantics invented.

---

## 1. Current state (verified)

### 1.1 `pnl()` — `app/services/__init__.py:668`

Returns flat dict:
```python
{"total_income": Decimal, "total_expense": Decimal, "net_profit": Decimal}
```

Semantics (verified in code):
- `OperationType.kind == "INCOME"` → income (amounts stored positive, §27)
- `OperationType.kind == "EXPENSE"` → expense (amounts stored negative, negated to positive)
- `OperationType.kind == "TRANSFER"` → excluded (§28, §63, §76)
- `Operation.status == "POSTED"` only (§16)
- `Operation.accrual_date IS NOT NULL` only, filtered to period (accrual basis)

### 1.2 `v_ip_financial_summary` — `database/schema.sql:2235`

Flat columns per (IP, month): `total_income`, `total_expense`, `net_profit`,
plus per-code breakdowns: `revenue_kaspi_shop`, `revenue_kaspi_pay`,
`expense_material`, `expense_kaspi_fee`, `expense_salary`,
`transfers_in`, `transfers_out`, `operations_count`.

### 1.3 `operation_types` — verified live in DB (2026-10-04)

| code | kind | direction | affects_pnl |
|------|------|-----------|-------------|
| SALE_KASPI_SHOP | INCOME | IN | true |
| SALE_KASPI_PAY | INCOME | IN | true |
| EXPENSE_MATERIAL | EXPENSE | OUT | true |
| EXPENSE_KASPI_FEE | EXPENSE | OUT | true |
| SALARY_PAYMENT | EXPENSE | OUT | true |
| TRANSFER_IN | TRANSFER | IN | false |
| TRANSFER_OUT | TRANSFER | OUT | false |

Columns available: `id, code, name, direction, kind, affects_pnl, is_system,
is_active, created_at, updated_at`. **There is no `cost_layer` column.**

Constraints (verified): `chk_operation_types_dir_kind` —
INCOME⇒IN, EXPENSE⇒OUT, TRANSFER⇒any direction.

### 1.4 `categories` — 12 seeded (verified live)

`INCOME_KASPI_SHOP, INCOME_KASPI_PAY, INCOME_OTHER,
EXPENSE_MATERIALS, EXPENSE_KASPI_FEE, EXPENSE_DELIVERY, EXPENSE_SALARY,
EXPENSE_PRODUCTION, EXPENSE_RENT, EXPENSE_UTILITIES, EXPENSE_ADVERTISING,
EXPENSE_OTHER`

`operations.category_id` exists and is set by ETL mapping.

### 1.5 Book requirement — `docs/BOOK_SYNTHESIS_2026-10-03.md`

Layered P&L (GAP-2A/2B): маржинальный доход (revenue − direct) →
операционная прибыль (− overhead) → чистая прибыль (− depreciation).
Book models overhead separately, **never invents allocation** (OQ-010 stays).

---

## 2. Layer definitions from existing data

Target structure (7 layers per task §3):

| # | Layer | Formula | Data source (exact) |
|---|-------|---------|---------------------|
| 1 | **Revenue** | `SUM(o.amount)` | `operations.amount` WHERE `operation_types.kind='INCOME'` AND `o.status='POSTED'` AND `o.accrual_date IS NOT NULL` AND period filter on `accrual_date` |
| 2 | **Direct Cost** | `SUM(-o.amount)` | `operations.amount` WHERE `operation_types.code IN ('EXPENSE_MATERIAL','EXPENSE_KASPI_FEE')` AND same POSTED/accrual filters. See §3 for why exactly these two. |
| 3 | **Gross Margin** (маржинальный доход) | `Revenue − Direct Cost` | Derived. No new data. |
| 4 | **Operating Expenses** | `SUM(-o.amount)` | `operations.amount` WHERE `operation_types.kind='EXPENSE'` AND `code NOT IN ('EXPENSE_MATERIAL','EXPENSE_KASPI_FEE')` AND same filters. **Reported as a single unallocated line** — see §5. |
| 5 | **Operating Result** (операционная прибыль) | `Gross Margin − Operating Expenses` | Derived. |
| 6 | **Other Income/Expenses** | — | **EMPTY in current schema.** No operation_type codes exist for non-operating items (no depreciation, no FX, no asset sale codes). Layer present in structure, always 0. Reserved for future codes. |
| 7 | **Net Result** (чистая прибыль) | `Operating Result ± Other` | Derived. Must equal current `net_profit` (reconciliation invariant, §7). |

**Key identity:** `Revenue − Direct Cost − Operating Expenses = Net Result`
must equal existing `total_income − total_expense = net_profit`, because
layers 2+4 partition the existing EXPENSE set exhaustively:
`{EXPENSE_MATERIAL, EXPENSE_KASPI_FEE} ∪ {all other EXPENSE codes} = all EXPENSE codes`.

### Per-code revenue drill-down (already exists in view)

`revenue_kaspi_shop` (`SALE_KASPI_SHOP`), `revenue_kaspi_pay` (`SALE_KASPI_PAY`).
Layer 1 can expose `revenue_by_source: {kaspi_shop, kaspi_pay, other}` where
`other = total_income − kaspi_shop − kaspi_pay`.

---

## 3. Direct vs needs-owner classification

### 3.1 OBJECTIVELY direct (no owner decision needed)

| Code | Reason it is direct (evidence, not opinion) |
|------|----------------------------------------------|
| `EXPENSE_MATERIAL` | Materials consumed by production. `order_direct_cost()` (`app/services/__init__.py:154`) already treats `order_materials.total_cost` as the material part of direct cost per order (§37, §49). Cost scales with production volume. |
| `EXPENSE_KASPI_FEE` | Kaspi sales commission. Charged per sale transaction (percentage of turnover); zero sales ⇒ zero commission. Directly varies with revenue. Kaspi's own docs: commission is deducted automatically per sale and visible in the Kaspi Pay statement. |

Both are variable costs in the management-accounting sense: they would not
exist without the corresponding revenue/production event.

### 3.2 MIXED — cannot classify without owner input

| Code | Why ambiguous |
|------|---------------|
| `SALARY_PAYMENT` | Posted by `pay_payroll_summary()` (`app/services/__init__.py:471`) as ONE lump operation per summary. The summary aggregates **piecework** (direct labour — scales with orders, `work_entries` CONFIRMED) **and** base salary + bonuses + deductions (potentially fixed/overhead). The ledger operation does not split components. Classifying all of it as direct would misstate margin for IPs with salaried staff; classifying all as overhead would understate direct cost for piecework-heavy production. **Needs owner: split payroll posting by component, or classify whole.** |

### 3.3 OBJECTIVELY operating (unallocated until OQ-010)

Any future EXPENSE codes (rent, utilities, advertising, delivery — categories
`EXPENSE_RENT`, `EXPENSE_UTILITIES`, `EXPENSE_ADVERTISING`, `EXPENSE_DELIVERY`
already seeded in `categories` but have no operation_type codes yet) are
operating expenses by default. They sit in layer 4 as an **unallocated total**
until OQ-010 defines allocation rules. No allocation is performed; the report
shows "operating expenses (unallocated)".

### 3.4 What about `categories`?

`operations.category_id` provides a second classification axis (12 categories),
but category assignment comes from ETL mapping, not from a controlled
posting rule. The layered P&L keys off `operation_types.code` (system-controlled,
CHECK-constrained) as the primary axis. Categories remain available for
drill-down (`expense by category` = GAP-007, P2) but are NOT the layer key.

---

## 4. Proposed `pnl()` return structure

**Additive, backward-compatible.** Existing keys keep exact values.

```python
{
    # --- existing keys, UNCHANGED semantics ---
    "total_income": Decimal,    # = revenue (layer 1)
    "total_expense": Decimal,   # = direct_cost + operating_expenses
    "net_profit": Decimal,      # = net_result (layer 7)

    # --- new layered keys ---
    "revenue": Decimal,                    # layer 1
    "revenue_by_source": {
        "kaspi_shop": Decimal,             # ot.code = 'SALE_KASPI_SHOP'
        "kaspi_pay": Decimal,              # ot.code = 'SALE_KASPI_PAY'
        "other": Decimal,                  # total_income - shop - pay
    },
    "direct_cost": Decimal,                # layer 2
    "direct_cost_by_code": {
        "material": Decimal,               # ot.code = 'EXPENSE_MATERIAL'
        "kaspi_fee": Decimal,              # ot.code = 'EXPENSE_KASPI_FEE'
    },
    "gross_margin": Decimal,               # layer 3 = revenue - direct_cost
    "gross_margin_pct": Decimal,           # gross_margin / revenue * 100 (0 if revenue=0)
    "operating_expenses": Decimal,         # layer 4 (unallocated total)
    "operating_expenses_by_code": {        # dynamic: {code: amount} for all
        ...                                # EXPENSE codes not in direct set
    },
    "salary_unclassified": Decimal,        # subset of operating_expenses:
                                           # ot.code = 'SALARY_PAYMENT'
                                           # (flagged: needs owner classification, §3.2)
    "operating_result": Decimal,           # layer 5 = gross_margin - operating_expenses
    "other_income": Decimal,               # layer 6a = 0 (no codes exist)
    "other_expenses": Decimal,             # layer 6b = 0 (no codes exist)
    "net_result": Decimal,                 # layer 7 = operating_result + other
                                           # MUST equal net_profit
}
```

### Implementation notes (for the future implementer)

1. **No schema change required.** Classification is a code→layer map in the
   service, not a new column. (GAP report suggested `cost_layer` column;
   this design deliberately avoids it — the 7-code set is CHECK-constrained
   and `is_system`; a column adds migration + sync risk for zero benefit
   at current scale. Revisit if operation_types grows beyond ~20 codes.)
2. **Direct set is a named constant**, e.g.
   `DIRECT_COST_CODES = frozenset({"EXPENSE_MATERIAL", "EXPENSE_KASPI_FEE"})`,
   with a comment citing this doc §3.1.
3. **All filters mirror existing `pnl()`**: `status='POSTED'`,
   `accrual_date IS NOT NULL`, period on `accrual_date`, `ip_id` scope.
4. **Transfers excluded** exactly as today (`kind='TRANSFER'` never enters).
5. `salary_unclassified` is informational: it does NOT change totals; it
   surfaces the classification ambiguity to the user instead of hiding it.
6. `v_ip_financial_summary` does NOT need new columns for this — the service
   computes layers; the view keeps its flat shape (or gains layer columns
   later as a separate change).

### What is deliberately NOT in the structure

- No `overhead_allocated` key — allocation is OQ-010 blocked.
- No `ebitda` / `depreciation` — no depreciation codes exist; inventing
  them would be inventing accounting semantics.
- No per-order margin — that is `order_gross_profit()` territory (§37),
  a different report.

---

## 5. What stays OWNER decision (explicitly)

| # | Question | Why owner | What works without it |
|---|----------|-----------|----------------------|
| OQ-010 | Overhead allocation method (by revenue? by direct cost? by labour hours? by order?) | Changes reported per-order/per-IP profitability; a business rule, not a technical fact | Layered P&L with operating expenses as **unallocated total** (this design) |
| OQ-SALARY | Is `SALARY_PAYMENT` direct, overhead, or split by component (piecework vs base/bonus)? | Changes gross margin materially; payroll structure is a business decision | Report salary as `salary_unclassified` inside operating expenses, flagged |
| (future) | Depreciation policy, other income/expense codes | No codes exist; defining them creates accounting policy | Layer 6 present but zero |

**The design rule:** the report shows what the data objectively supports and
labels what it cannot classify, instead of silently misclassifying.

---

## 6. GAP-004 interaction (date semantics)

Layered P&L inherits the existing `pnl()` date rule: **accrual basis**,
`accrual_date IS NOT NULL`, period filter on `accrual_date`. This is the
correct P&L semantic (book: ОПУ by accrual month). The GAP-004 issue
(`/operations/summary` uses `operation_date` vs `/pnl` uses `accrual_date`)
is a **documentation/API-contract matter**, not a bug in either endpoint:
- `/operations/summary` = ledger browser → `operation_date` (when cash moved)
- `/pnl` (flat or layered) = P&L → `accrual_date` (when earned/incurred)

The layered design does not change this. A future `date_basis` parameter
(GAP-004 proposal) would apply identically to flat and layered P&L.

---

## 7. Test plan

### 7.1 Reconciliation invariant (the core test)

For any dataset: `net_result == net_profit` AND
`revenue == total_income` AND `direct_cost + operating_expenses == total_expense`.

This is provable by construction (layers partition the EXPENSE code set),
but must be asserted in tests against real DB rows, not just trusted.

### 7.2 Layer correctness

Fixture: IP with POSTED operations in a period —
- `SALE_KASPI_SHOP` +100000 (accrual_date in period)
- `EXPENSE_MATERIAL` −30000
- `EXPENSE_KASPI_FEE` −5000
- `SALARY_PAYMENT` −20000
- `TRANSFER_IN` +50000 (must not appear anywhere in P&L)
- DRAFT `SALE_KASPI_PAY` +99999 (must not appear — §16)

Expected:
- `revenue` = 100000, `revenue_by_source.kaspi_shop` = 100000
- `direct_cost` = 35000 (`material`=30000, `kaspi_fee`=5000)
- `gross_margin` = 65000, `gross_margin_pct` = 65.00
- `operating_expenses` = 20000, `salary_unclassified` = 20000
- `operating_result` = 45000
- `other_income` = `other_expenses` = 0
- `net_result` = 45000 = `net_profit` ✓
- `total_expense` = 55000 = 35000 + 20000 ✓

### 7.3 Edge cases

- Empty period → all zeros, `gross_margin_pct` = 0 (no division by zero).
- Revenue = 0 with expenses → `gross_margin_pct` = 0, margin negative.
- Cross-IP: layers respect `ip_ids` scope (no leakage).
- `accrual_date IS NULL` operations excluded from all layers (no double-count).
- New EXPENSE code added later (e.g. `EXPENSE_RENT`) → automatically lands
  in `operating_expenses_by_code`, totals still reconcile (open-closed test:
  insert a new EXPENSE code, assert invariant holds without code change).

### 7.4 API test

`GET /pnl` returns all new keys; Decimals serialized as strings (existing
convention). Restricted user sees only allowed IPs' layers.

### 7.5 What is NOT tested (blocked)

- Overhead allocation correctness — no allocation exists to test (OQ-010).
- Salary split — `salary_unclassified` asserted as informational only.

---

## 8. Summary for parent agent

| Question | Answer |
|----------|--------|
| Can GAP-001 structure be built without OQ-010? | **Yes.** 6 of 7 layers fully definable; overhead appears as unallocated total. |
| Direct codes (objective)? | `EXPENSE_MATERIAL`, `EXPENSE_KASPI_FEE` (§3.1 evidence). |
| Needs owner? | `SALARY_PAYMENT` split (piecework vs base/bonus); OQ-010 allocation method. |
| Schema change needed? | No. Code→layer map in service; revisit if codes grow. |
| Reconciliation invariant? | `net_result == net_profit` by construction; must be test-asserted. |
| Depreciation/EBITDA? | Not defined — no codes exist; do not invent. |
| Next step if approved | Implement per §4 + §7, then commit. |

**Classification of this deliverable:** DESIGN ONLY (no code changed, per task).
Recommended severity of remaining work: P1 (structure) — implementable now;
the OQ-010-dependent allocation stays OWNER BLOCKED as before.
