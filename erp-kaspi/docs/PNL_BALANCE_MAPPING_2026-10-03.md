# P&L Classification & Management Balance Source Mapping (2026-10-03)

**Scope**: analysis only, no code changes. Maps erp-kaspi data sources to the book's
P&L layers and management-balance elements. Book = «Excel для финансового директора»
(Актион, 2015) as management-architecture source only; no KZ tax rules invented.

## PART A — P&L classification

### Current classification mechanism

`operation_types` (schema.sql:784) has 4 classification-relevant fields:

| Field | Values | Purpose |
|---|---|---|
| `direction` | IN / OUT | drives sign of `operations.amount` (§27) |
| `kind` | INCOME / EXPENSE / TRANSFER | P&L category system (§21) |
| `affects_pnl` | BOOLEAN | FALSE for transfers (§28, §76) |
| `is_system` | BOOLEAN | 7 mandatory types from §20 |

There is **no** `direct/overhead` flag, **no** P&L section field, **no** margin-layer field.

### Current P&L computation

- `v_ip_financial_summary` (schema.sql:2097): `total_income` = Σ(INCOME), `total_expense` = Σ(EXPENSE),
  `net_profit` = income − expense. Flat — no layers.
- `pnl()` (app/services/__init__.py:600): mirrors the view. POSTED-only.

### System operation types (7, schema.sql:2433)

| Code | direction | kind | affects_pnl |
|---|---|---|---|
| SALE_KASPI_SHOP | IN | INCOME | TRUE |
| SALE_KASPI_PAY | IN | INCOME | TRUE |
| EXPENSE_MATERIAL | OUT | EXPENSE | TRUE |
| EXPENSE_KASPI_FEE | OUT | EXPENSE | TRUE |
| SALARY_PAYMENT | OUT | EXPENSE | TRUE |
| TRANSFER_IN | IN | TRANSFER | FALSE |
| TRANSFER_OUT | OUT | TRANSFER | FALSE |

### Book P&L layers → erp-kaspi mapping (proposed, pending OQ-010)

Book chain (p. 30): выручка − прямые затраты = **маржинальный доход**;
− накладные = **операционная прибыль**; − амортизация = **чистая прибыль**.

| Operation type / source | P&L section (proposed) | Basis |
|---|---|---|
| SALE_KASPI_SHOP | revenue | kind=INCOME, sales |
| SALE_KASPI_PAY | revenue | kind=INCOME, sales |
| EXPENSE_MATERIAL | direct_cost | materials consumed per order (§37, `order_materials.total_cost`) |
| EXPENSE_KASPI_FEE | direct_cost | commission directly tied to sale (arguable; needs owner confirmation) |
| SALARY_PAYMENT (piecework) | direct_cost | piecework = variable labor tied to orders |
| SALARY_PAYMENT (fixed salary) | overhead | fixed salaries — **cannot distinguish today**: one type covers both |
| EXPENSE_* (rent, utilities, advertising) | overhead | categories exist (`EXPENSE_RENT`, `EXPENSE_UTILITIES`, `EXPENSE_ADVERTISING`) but no operation types force their use |
| Depreciation | — | **no source**: no fixed-assets table, no depreciation postings |
| Financing (loans interest) | financing | **no source**: no loans table |
| TRANSFER_IN / TRANSFER_OUT | excluded | affects_pnl=FALSE (§28) — correct |

### Unclassifiable / missing (gap list)

1. **SALARY_PAYMENT mixes piecework and fixed salary** — one type, two P&L layers.
   Needed: separate types or a `cost_nature` (direct/overhead) flag. OQ-010 adjacent.
2. **No direct/overhead flag anywhere** — `operation_types` and `categories` have no
   cost-nature attribute. The mapping above is by convention, not enforced.
3. **Depreciation has no source** — no `fixed_assets` table, no depreciation operations.
4. **Financing section has no source** — no loans, no interest operations.
5. **EXPENSE_KASPI_FEE as direct_cost is a judgment call** — commission is sale-linked,
   but owner should confirm (direct vs selling-expense).
6. **Categories are advisory** — nothing forces an operation to carry the right category;
   `categories.kind` must match type kind (§21) but cost-nature is unmodeled.

**OQ-010 note**: overhead allocation is NOT invented here. The mapping keeps overhead as a
separate configured layer (book pattern); allocation rules remain an owner decision.

## PART B — Management balance source mapping

For a furniture manufacturer (book's service example does not apply directly:
inventory/WIP/finished goods are real here).

| Balance element | Source status | Table / derivation |
|---|---|---|
| Cash | EXISTING / DERIVABLE | `accounts` (KASPI_PAY/KASPI_SHOP/BANK/CASH/OTHER per ip_id) + `cash_flow()` ending_balance |
| AR | NEW SOURCE REQUIRED (derivable post-accrual) | No AR table. Derivable via book formula (opening + accruals − payments) **only after `accrual_date` exists**. Counterparties exist for analytics. |
| Inventory (materials) | NEW SOURCE REQUIRED | `materials` is a catalogue (sku/name/price) — **no stock quantities**, no stock ledger. `order_materials` records consumption only. |
| WIP | DERIVABLE (partial) | `orders` with in-production statuses + `order_materials` consumed + `work_entries` labor. No WIP valuation rule — needs owner decision on costing basis. |
| Finished Goods | NEW SOURCE REQUIRED | `products` is a catalogue (no quantity). No FG stock tracking. |
| Fixed Assets | NEW SOURCE REQUIRED | No table at all. |
| AP | NEW SOURCE REQUIRED (derivable post-accrual) | Same as AR, supplier side. `counterparties` exist. |
| Payroll liabilities | DERIVABLE | `payroll_summary.total_calculated` − Σ(`payroll_payments`) per employee/period. No overpay possible (DB guard 0013). |
| Taxes / other liabilities | NEW SOURCE REQUIRED | `tax_rates_history` exists (rates only) — no tax liability postings. |
| Loans | NEW SOURCE REQUIRED | No table. |
| Equity | DERIVABLE (once above exist) | Equity = Assets − Liabilities; no separate equity table needed initially (book pattern: derived). |

### Manufacturing-specific notes (where the book's service example doesn't apply)

- **Inventory/WIP/FG are the hard part** — the book's service company has none.
  Furniture production needs a stock model (materials in/out, WIP valuation, FG).
  This is a P1–P2 project in itself, not a quick add.
- **Depreciation needs fixed assets first** — can't post depreciation without the FA register.
- **WIP valuation** depends on the costing decision (OQ-010 adjacent): direct cost is
  defined (§37: materials + piecework), but WIP cut-off rules are not.
- **Cash/AR/AP/payroll** follow the book directly and are the right first balance slice.

### Recommended balance build order

1. Cash (exists) → 2. AR/AP (needs accrual_date) → 3. Payroll liabilities (derivable) →
4. Inventory stock ledger (new) → 5. WIP/FG (needs costing rules) →
6. Fixed assets + depreciation (new) → 7. Loans/taxes (new) → 8. Equity (derived)
