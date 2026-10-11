# Book synthesis: management financial architecture gaps (2026-10-03)

**Source**: «Excel для финансового директора» (Актион, 2015) — analyzed as management-accounting architecture source only. Not a KZ regulatory source; no Excel formulas copied; no KZ tax/accounting rules invented.

**Companion reports**:
- `docs/BOOK_ANALYSIS_CASHFLOW_PNL_2026-10-03.md` (areas 1–5)
- `docs/BOOK_ANALYSIS_ARAP_TREASURY_2026-10-03.md` (areas 6–11)
- `docs/BOOK_ANALYSIS_BUDGET_CONSOLIDATION_2026-10-03.md` (areas 12–16)

## Critical path (P1, implementable without owner decisions)

1. **accrual_date** (GAP-4A): Add `accrual_date` to `operations`. Book: Дата начисления + Дата оплаты in one register; ОДДС by payment month, ОПУ by accrual month. Currently P&L is cash-basis. **Blocks TD-024 (AR/AP)**. Owner decision needed only for NULL-rule/backfill policy.

2. **Layered P&L** (GAP-2A/2B): маржинальный доход (revenue − direct) → операционная прибыль (− overhead) → чистая прибыль (− depreciation). Requires direct/overhead classification. Currently flat income/expense/net.

3. **Management balance** (GAP-3A/3B): Derived from P&L + cash flow + opening balance (book pp. 32–35). No extra register columns needed. Requires opening-balance storage.

4. **AR/AP model** (GAP-6.1): сальдо взаиморасчетов = opening + accruals − payments. Blocked on accrual_date.

5. **Treasury statuses** (GAP-10.1): зарег. → план → факт. Currently only POSTED/REVERSED/CORRECTED/DRAFT.

## P2 (requirement candidates, not in FINANCE-OPS-001)

- Cash-flow sections (operating/financing/investing), article reference table
- Budget vs Actual (budget_versions + budget_lines), overrun control
- Cost centers (ЦФО) below ИП
- AR/AP aging buckets, debt dynamics, drill-down endpoints

## Owner decisions (not invented)

- **TD-023 (turnover)**: Book does NOT define «оборот» as KPI. Stays OWNER DECISION with candidate formulas A/B/C.
- **TD-022 (dashboard)**: Book has no dashboard concept. Recommends: pair every report with a chart (especially daily dynamics), single-dimension analytical reports, cell-level drill-down.
- **OQ-010 (overhead)**: Book models overhead separately, never invents allocation. Keep Direct Cost until answered.
- **Payroll posting basis**: accrual vs cash — requirements review needed before code change.
- **AR/AP semantics, aging buckets, treasury design**: all flagged as BUSINESS decisions.

## Implementation order

accrual_date → direct/overhead → layered P&L → article table → sectioned cash flow → opening balances → derived balance → AR/AP → treasury statuses → budgets/ЦФО
