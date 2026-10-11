# P1 Financial Correctness — Gap Search 2026-10-04

**Scope:** Can ERP answer the money questions? Chain verified: Requirement → Service → API → Test.

**Method:** Read `app/services/__init__.py` (pnl, cash_flow), `database/schema.sql` (views), `app/api/__init__.py` (endpoints). Checked each question for working end-to-end chain.

**Out of scope (per task):** accrual foundation (closed), TD-023 turnover (owner), OQ-010 overhead (owner), REPORT #8 (owner).

---

## Questions that WORK today

| # | Question | Chain | Evidence |
|---|----------|-------|----------|
| 1 | How much earned? (revenue by period, by IP) | ✅ `/pnl` → `pnl()` → accrual_date filter | `app/services/__init__.py:606`, `app/api/__init__.py:1746` |
| 2 | How much spent? (total expenses) | ✅ `/pnl` → `pnl()` | Same as above; `total_expense` returned |
| 3 | Where did money go? (cash flow) | ✅ `/reports/cash-flow` → `cash_flow()` DB function | `app/api/__init__.py:1013`, `schema.sql` cash_flow function |
| 4 | Net income? | ✅ `/pnl` → `net_profit` | `pnl()` returns `income - expense` |
| 5 | Per-IP breakdown? | ✅ `/reports/ip-summary` → `v_ip_financial_summary` | `app/api/__init__.py:1043`, monthly per-IP |
| 7 | Revenue by source (Kaspi Shop vs Pay)? | ✅ `v_ip_financial_summary` has `revenue_kaspi_shop`, `revenue_kaspi_pay` | `schema.sql:2145-2146` |

---

## GAP-001: No layered P&L (revenue→direct→margin→overhead→operating→net)

**Severity:** P1 (financial correctness — management decisions need margin visibility)

**What's missing:** Service, API, test. `pnl()` returns flat `{total_income, total_expense, net_profit}`. No gross margin, no operating result layers.

**Evidence:**
- `app/services/__init__.py:606-676` — only 3 keys returned
- `v_ip_financial_summary` — flat columns, no margin layers
- Book requires layered P&L (BOOK_SYNTHESIS_2026-10-03): revenue → direct cost → margin → overhead → operating → net

**Business impact:** Cannot answer "what is our margin?", "are we profitable after direct costs?", "what is operating result?". Management flies blind on profitability structure.

**Proposed fix:** Add `direct_cost` classification to operation_types (new column `cost_layer`: DIRECT/OVERHEAD/OTHER), extend `pnl()` to return layered breakdown. Does NOT require owner — direct cost mapping is technical (EXPENSE_MATERIAL, SALARY_PAYMENT are objectively direct). Overhead allocation (OQ-010) stays owner decision, but the *layer structure* can be built now with overhead as unallocated.

**Tests required:** Unit test for layered pnl(), API test for `/pnl` layered response, regression that flat totals still match.

**Owner decision needed?** NO for structure. YES for overhead allocation rules (already OQ-010).

---

## GAP-002: No per-bank-account cash breakdown

**Severity:** P1 (treasury — "where is my money RIGHT NOW by account?")

**What's missing:** Service, API, test. `accounts` table exists (KASPI_PAY, KASPI_SHOP, BANK, CASH, OTHER per IP). `operations.account_id` exists. But `cash_flow()` only accepts `p_ip_id` — no account parameter. No endpoint answers "how much cash in Kaspi Pay vs bank vs cash?"

**Evidence:**
- `database/schema.sql:1095` — `accounts` table defined
- `cash_flow()` signature: `(p_ip_id INT, p_period_start DATE, p_period_end DATE)` — no account filter
- `app/api/__init__.py:1283` — `/accounts` is reference CRUD only, no balances
- Zero references to `account_id` in API layer (grep returns nothing)

**Business impact:** Cannot answer "which account holds the cash?", "is Kaspi Pay balance sufficient for payroll?", "where did the transfer go?". Treasury management impossible.

**Proposed fix:** Add `p_account_id INT DEFAULT NULL` to `cash_flow()`, add `/reports/cash-flow/by-account` endpoint returning per-account balances. Account balances = SUM(amount) WHERE account_id = X AND status='POSTED'.

**Tests required:** Per-account cash flow test, transfer between accounts test (source decreases, dest increases).

**Owner decision needed?** NO — technical.

---

## GAP-003: v_counterparty_balances has no API endpoint

**Severity:** P1 (AR/AP visibility — the view exists but is unreachable)

**What's missing:** Service function, API endpoint, test. `v_counterparty_balances` (AR/AP Tier 1) is defined in schema.sql but has zero API exposure.

**Evidence:**
- `database/schema.sql:2614` — view defined with `total_accrued`, `total_settled`, `balance`
- `grep counterparty_balances app/api/__init__.py` — zero results
- `grep counterparty_balances app/services/__init__.py` — zero results

**Business impact:** Cannot answer "who owes us?", "whom do we owe?" via API. The data exists in DB but is invisible to UI/users.

**Proposed fix:** Add `get_counterparty_balances(session, ip_id)` service + `GET /reports/counterparty-balances?ip_id=X` endpoint. Filter positive=AR, negative=AP.

**Tests required:** AR/AP endpoint test with accrued-but-unpaid scenario.

**Owner decision needed?** NO — Tier 1 is already implemented as view; exposing it is technical.

---

## GAP-004: /operations/summary vs /pnl period semantic mismatch

**Severity:** P1 (reporting inconsistency — same period, different numbers)

**What's missing:** Consistent date semantics. `/operations/summary` filters by `operation_date`. `/pnl` filters by `accrual_date`. For cross-period accruals, the same date range shows different income/expense totals.

**Evidence:**
- `app/api/__init__.py:793-796` — `Operation.operation_date >= period_start`
- `app/services/__init__.py:654-658` — `Operation.accrual_date >= period_start`
- Financial regression doc flags this as "V2 unresolved"

**Business impact:** User sees "income = X" on operations page but "income = Y" on P&L page for same period. Erodes trust in reports. Support tickets.

**Proposed fix:** Add `date_basis` query param to `/operations/summary`: `accrual` (default for P&L consistency) or `cash` (explicit). OR document that /operations/summary is cash-basis ledger browser while /pnl is accrual-basis, with UI labels. The key is explicitness, not silent divergence.

**Tests required:** Cross-period test asserting both endpoints with documented semantics.

**Owner decision needed?** NO — this is a UX/semantics clarification, technically safe either way as long as explicit.

---

## GAP-005: No "accrued but not paid" obligation list

**Severity:** P1 (working capital — "what exactly is unpaid?")

**What's missing:** Service, API, test. `v_counterparty_balances` shows NET per counterparty, but cannot list individual unpaid obligations. If counterparty has 3 invoices (2 paid, 1 unpaid), the view nets them — user cannot see WHICH is unpaid.

**Evidence:**
- `v_counterparty_balances` GROUP BY counterparty — aggregates away invoice granularity
- No `due_date` column on operations (grep returns nothing)
- No obligation/document tracking

**Business impact:** Cannot answer "which specific invoices are overdue?", "what is our upcoming payment schedule?". Cash planning requires obligation-level detail.

**Proposed fix:** This is TD-024 Tier 2 (invoice-level AR/AP) — already identified as owner decision. However, an INTERIM step is technically safe: add `/reports/counterparty-balances` (GAP-003) first, which at least shows net per counterparty. Full obligation tracking needs owner decision on allocation policy.

**Tests required:** (After GAP-003) net balance test per counterparty.

**Owner decision needed?** PARTIAL — GAP-003 (net exposure) needs no owner. Full invoice-level (TD-024) already has owner decision pending.

---

## GAP-006: No prepayment/advance distinction

**Severity:** P2 (working capital accuracy)

**What's missing:** Data model, service, API. No concept of "paid in advance" vs "paid against obligation". A payment with no matching accrual is indistinguishable from a settlement.

**Evidence:**
- `grep -ri "advance\|prepay" app/services/ app/api/ database/schema.sql` — zero results (excluding tests)
- `v_counterparty_balances`: negative balance could be AP OR prepayment — ambiguous

**Business impact:** Cannot answer "what did we pay in advance?", "which prepayments are still unconsumed?". Affects working capital accuracy.

**Proposed fix:** Requires business semantics (is advance a separate operation type? a flag? a settlement_link with future accrual?). This is closely related to TD-024. Recommend: add as OWNER-DECISION alongside TD-024, OR implement minimal `is_advance` boolean on operations as infrastructure without business rules.

**Tests required:** TBD after semantic decision.

**Owner decision needed?** YES — advance treatment affects financial interpretation. Add to TD-024 decision pack.

---

## GAP-007: Expense breakdown by category not in P&L API

**Severity:** P2 (cost visibility)

**What's missing:** API, test. `categories` table exists with rich expense categories (materials, delivery, rent, utilities, advertising, etc.). `v_ip_financial_summary` hardcodes only 3 expense types (material, kaspi_fee, salary). `/pnl` returns only total_expense — no category breakdown.

**Evidence:**
- `database/schema.sql:2483` — 12 categories seeded
- `app/services/__init__.py:606` — `pnl()` has no category dimension
- `v_ip_financial_summary` — only `expense_material`, `expense_kaspi_fee`, `expense_salary` hardcoded

**Business impact:** Cannot answer "how much did we spend on rent?", "what are our advertising costs?" via P&L API. Category data is collected but not reported.

**Proposed fix:** Extend `pnl()` with optional `by_category=True` returning `{category_code: amount}`. Add to `/pnl` as query param. Technical, no business semantics needed.

**Tests required:** Category breakdown test.

**Owner decision needed?** NO.

---

## Summary

| Gap ID | Severity | Area | Owner needed? |
|--------|----------|------|---------------|
| GAP-001 | P1 | Layered P&L | No (structure); Yes for overhead alloc (OQ-010) |
| GAP-002 | P1 | Per-account cash | No |
| GAP-003 | P1 | AR/AP endpoint | No |
| GAP-004 | P1 | Date semantic consistency | No |
| GAP-005 | P1 | Unpaid obligations list | Partial (TD-024 for full) |
| GAP-006 | P2 | Prepayments | Yes (extend TD-024) |
| GAP-007 | P2 | Expense by category | No |

**Highest priority:** GAP-003 (AR/AP endpoint — view exists, just needs exposure), GAP-002 (treasury blind spot), GAP-001 (margin visibility).

**No duplicates:** Checked existing docs — no prior P1 financial gap doc found.
