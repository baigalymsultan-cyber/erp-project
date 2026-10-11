# GAP-004 — Date Semantics Audit — 2026-10-04

**Scope:** full inventory of date column usage in financial logic (`app/services/__init__.py`,
`app/api/__init__.py`, `database/schema.sql`, `etl/import_excel.py`, `app/models/`,
`app/schemas/__init__.py`, `frontend/`).

**Method:** read-only code inspection. No code changed.

**Verdict:** No financial-misstatement BUG found in date *selection* — every report uses the
semantically correct column. Found **1 contract bug** (documented promise the code cannot
deliver), **2 stale docstrings**, and the core **GAP-004 documentation gap** (date basis
unlabeled in API/UI).

---

## 1. Audit matrix — which date drives what

| # | Consumer | Date column used | Correct? | Verdict |
|---|----------|-----------------|----------|---------|
| 1 | `pnl()` service (`app/services/__init__.py:714-724`) | `accrual_date` (strict `IS NOT NULL`, period filter on `accrual_date`) | Yes — accrual-basis P&L | ✅ CORRECT |
| 2 | `v_ip_financial_summary` (`schema.sql:2235`) | P&L columns: `accrual_date IS NOT NULL`; `period_month = COALESCE(accrual_date, operation_date)` | Yes — pure-settlement rows contribute 0 to P&L cols; transfers are cash-basis by nature | ✅ CORRECT |
| 3 | `cash_flow()` (`schema.sql:2280`) | `operation_date` for period + start balance | Yes — cash moves on cash date | ✅ CORRECT |
| 4 | `GET /operations/summary`, `GET /operations` list (`api:535-537, 807-809`) | `operation_date` | Yes for a **ledger browser** — but totals are labeled `income`/`expense` with no basis qualifier | ⚠️ DOC GAP (D5) |
| 5 | `v_counterparty_balances` (`schema.sql:2714`) | No period filter; splits by `accrual_date IS [NOT] NULL` (lifetime net per counterparty) | Yes — Tier 1 AR/AP is a position, not a period report | ✅ CORRECT |
| 6 | `GET /reports/counterparty-balances`, `/reports/account-balances` | View / `operation_date` (account balances = cash position) | Yes | ✅ CORRECT |
| 7a | Payroll calculate (`payroll_period_totals`, `services:242-244`) | `WorkEntry.entry_date` within `payroll_periods.start_date..end_date` | Yes — accrual driver is the work date | ✅ CORRECT |
| 7b | `pay_payroll_summary()` (`services:513-522`) | `operation_date = payment_date`, `accrual_date = payment_date` | Cash-basis salary in P&L — **known limitation**, TD-020 frozen | ⚠️ KNOWN / DOC (see §3) |
| 8 | Kaspi `reconcile_payout_full()` | No date logic — classifies `kaspi_payout_items` vs `orders` by amounts/IDs | N/A (reconciliation is amount/identity-based) | ✅ CORRECT |
| 9 | `POST /kaspi/payouts` envelope | `payout_date` only; **no operation is created**, `operation_id` stays NULL (KASPI-002) | Envelope date = document/settlement date from Kaspi | ✅ CORRECT (capability gap, not date bug) |
| 10 | `cash_flow()` payout subquery | `o.operation_date` of the *linked* operation (`kp.operation_id = o.id`) | Correct *when* linkage is built; linkage has no writer yet | ⚠️ DEFERRED (KASPI-002) |
| 11 | Transfers (`add_transfer`, `services:1140,1150`) | `transfer_date` → `operation_date` on both legs; `accrual_date` NULL → excluded from P&L | Yes — transfers never hit P&L (§28) | ✅ CORRECT |
| 12 | `settlement_links` | `created_at` only — **no date logic**; allocations are assertions, paid/remaining always derived via `SUM(allocated_amount)` | Correct — links carry no independent date semantics | ✅ CORRECT |
| 13 | ETL `run_import` (`etl/import_excel.py:323-325`) | `accrual_date = operation_date` (cash basis default, documented "Excel date semantic is undefined; backfill policy applies") | Yes per documented policy | ✅ CORRECT (policy choice) |

---

## 2. Findings

### D1 — CONTRACT BUG (medium): `accrual_date=None` promise is unimplementable

**Files:**
- `app/services/__init__.py` (~line 654, `add_operation`): comment says
  *"Explicit None means pure payment/settlement (no P&L effect)"*, but the code is
  `accrual_date=data.accrual_date if data.accrual_date is not None else data.operation_date`
  — **every** `None` becomes `operation_date`.
- `app/schemas/__init__.py:74-76` (`OperationCreate`): *"Set to None explicitly for pure
  payments/settlements."*

**Why it's false:** Pydantic `Field(default=None)` collapses "omitted" and "explicit null"
into the same `None` before the service ever sees it. The documented distinction cannot exist.

**Impact:** There is **no API path** to create an `accrual_date IS NULL` operation.
Correct AR/AP settlement recording via the generic API is impossible; callers must either
misuse `accrual_date = operation_date` (polluting P&L with settlement-only rows) or go to
direct SQL. `settlement_links` DI-002 validation (accrual leg must have `accrual_date`)
therefore cannot be satisfied through the public API for the payment leg either.

**Fix options (for implementation wave, not this audit):**
- A: introduce an explicit sentinel (e.g. `accrual_date: date | None` stays, but a separate
  boolean `is_settlement_only` or a distinct endpoint for settlements);
- B: change default to NULL (accrual opt-in) — **breaking**: all existing API-created ops
  would vanish from P&L; requires migration/backfill policy and owner sign-off.

### D2 — STALE DOCSTRING: `GET /operations/daily` (`app/api/__init__.py:588`)

Docstring: *"day = date(operation_date)"*. The SQL actually does
`SELECT o.accrual_date::date AS day ... WHERE o.accrual_date IS NOT NULL AND o.accrual_date BETWEEN ...`.
Since the endpoint is the daily P&L series (replacing `pnl_dataframe`), `accrual_date` is the
**correct** column — the docstring is wrong and could mislead a future "fix" into breaking it.

### D3 — STALE MODEL COMMENT: `Operation.accrual_date` (`app/models/finance.py:233-239`)

Comment claims *"P&L aggregates by COALESCE(accrual_date, operation_date)"*.
False: both `pnl()` and `v_ip_financial_summary` use strict `accrual_date IS NOT NULL`.
The comment describes a design that was **not** adopted.

### D4 — DOC GAP: `GET /reports/cash-flow` basis unlabeled

`cash_flow()` filters on `operation_date` (correct), but neither the endpoint docstring nor the
response says so. A user comparing `/pnl` vs `/reports/cash-flow` for the same period sees
different numbers with no explanation.

### D5 — DOC GAP (core GAP-004): `/operations/summary` vs `/pnl` show different numbers

`/operations/summary` totals `income`/`expense` by **`operation_date`** (cash basis);
`/pnl` totals by **`accrual_date`**. For any cross-period accrual the same date range yields
different figures. Both are correct for their purpose (ledger browsing vs P&L), but nothing
tells the user which basis they are looking at — API field names, docstrings, and UI labels
("С даты" / "По дату") are basis-agnostic.

### D6 — KNOWN LIMITATION (documented, TD-020 frozen): payroll is cash-basis in P&L

`pay_payroll_summary()` posts `SALARY_PAYMENT` with `accrual_date = payment_date`.
Salary for January paid in February hits **February** P&L, not January.
`payroll_periods.start_date/end_date` exist but are never used for P&L recognition.
Changing this requires the TD-020 payroll-lifecycle owner decision — do not implement here.

---

## 3. Entity date inventory

| Entity | event date | operation date | accrual date | settlement/payment date | document date | posting date | Missing |
|--------|-----------|----------------|--------------|------------------------|---------------|--------------|---------|
| `operations` | `operation_date` (cash/economic event) | same column | `accrual_date` NULL = pure settlement | — (settlement is a separate op or `settlement_links`) | — | `created_at` | `document_date` |
| `transfers` | `transfer_date` | legs inherit `transfer_date` | NULL (never P&L) | = transfer_date | — | `created_at` | — |
| `payroll_periods` | — | — | `start_date..end_date` (accrual period concept, unused by P&L — D6) | — | — | `created_at`, `closed_at` | `ip_id` (PAY-003/SEC-003) |
| `payroll_payments` | — | — | — | `payment_date` | — | `created_at` | — |
| `payroll_summary` | — | — | via period | via `payroll_payments` | — | `created_at` | own date (by design) |
| `work_entries` | `entry_date` (also accrual driver) | — | — | — | — | `created_at` | — |
| `kaspi_payouts` | — | — (no op linked yet) | — | `payout_date` (Kaspi document date) | `payout_date` | `created_at` | operation linkage writer (KASPI-002) |
| `kaspi_payout_items` | — | — | — | via payout | — | `created_at` | no creation path (KASPI-002) |
| `settlement_links` | — | — | — | — | — | `created_at` | none needed (assertion table) |
| `orders` | `order_date` (TIMESTAMPTZ) | — | — | — | `order_date` | `created_at` | — |
| `order_materials` | `used_at` | — | `price_at_time` frozen (§38) | — | — | `created_at` | — |
| `import_batches` | — | — | — | — | `batch_name`/file | `started_at`/`finished_at` | raw payload content (F-ETL-005) |

**Note on `kaspi_payouts.payout_date`:** it is the date Kaspi reports the settlement (document
date). When the KASPI-002 operation linkage is eventually built, the linked operation should use
`operation_date = payout_date` and `accrual_date = NULL` (cash receipt of previously accrued
sales — must not double-count P&L). Commission (`commission_amount`) needs its own
`EXPENSE_KASPI_FEE` operation with `accrual_date = payout_date` (KASPI-003), never netted.

---

## 4. Proposed doc text + UI labels (GAP-004 remediation, no code-logic change)

**API docstring convention** (add to every date-filtered endpoint):

> `period_start`/`period_end` filter by **{basis}**: `operation_date` = date money moved
> (cash basis), `accrual_date` = date the economic event is recognized (P&L basis).
> `/pnl` and `/operations/daily` use accrual basis; `/operations/summary`, `/operations`,
> `/reports/cash-flow` use cash basis.

**Endpoint renames/labels (RU/KZ/EN):**
- `/operations/summary` totals → label "Обороты (кассовый метод)" / "Айналым (кассалық әдіс)" /
  "Turnover (cash basis)" in UI; keep API field names (no breaking change).
- `/pnl` → UI subtitle "метод начисления" / "есептеу әдісі" / "accrual basis".
- `/reports/cash-flow` → UI subtitle "по дате движения денег" / "ақша қозғалысы күні бойынша" /
  "by cash movement date".

**Fix list for implementation wave:**
1. D1: decide A vs B (recommendation: A — explicit settlement flag/endpoint; B is breaking).
   Until then, correct the false comments in `add_operation` and `OperationCreate`.
2. D2: fix `/operations/daily` docstring → "day = date(accrual_date)".
3. D3: fix `Operation.accrual_date` model comment → strict `accrual_date IS NOT NULL`.
4. D4/D5: add basis notes to `/reports/cash-flow`, `/operations/summary`, `GET /pnl` docstrings
   + UI labels above.

---

## 5. Classification summary

| ID | Type | Severity | Financial misstatement? |
|----|------|----------|------------------------|
| D1 | Contract bug (false promise) | Medium | Indirect — blocks correct settlement recording via API |
| D2 | Stale docstring | Low | No (SQL is correct) |
| D3 | Stale model comment | Low | No (code is correct) |
| D4 | Documentation gap | Low | No |
| D5 | Documentation gap (GAP-004 core) | Medium | No — but unexplained number differences erode trust |
| D6 | Known limitation (TD-020) | Medium | By-design until owner decides |

**Bottom line:** the date *semantics* of the system are sound — accrual for P&L, cash date for
cash flow and ledger browsing, NULL-accrual for pure settlements, frozen prices for costing.
What is missing is honest labeling: one unimplementable contract (D1), two stale comments
(D2, D3), and basis-agnostic API/UI surfaces (D4, D5).
