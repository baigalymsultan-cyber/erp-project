# Financial Regression — accrual_date vs operation_date semantics

**Date:** 2026-10-04
**Scope:** read-only code verification + one new service-level regression test
**Product code:** NOT changed (per task constraint)

Rule under test (book-derived, user-confirmed):

- `accrual_date` → P&L (date of economic event)
- `operation_date` → Cash Flow (date cash moved)
- a payment of an already-accrued item must NOT duplicate P&L

---

## 1. `pnl()` uses `accrual_date` — VERIFIED

`app/services/__init__.py`, `pnl()` (~lines 653–662):

```python
stmt = stmt.where(Operation.accrual_date.is_not(None)).where(
    Operation.accrual_date >= period.period_start
).where(
    Operation.accrual_date <= period.period_end
)
```

- Strict `accrual_date IS NOT NULL`: pure payments/settlements never hit P&L.
- Status filter `Operation.status == "POSTED"` agrees with `v_ip_financial_summary`.
- Transfers excluded via `OperationType.kind.in_(("INCOME", "EXPENSE"))`.
- Matches the view semantics; no `COALESCE` fallback.

## 2. Cash Flow uses `operation_date` — VERIFIED

`alembic/versions/20261003_0012_cash_flow_function.py`, `cash_flow(p_ip_id, p_period_start, p_period_end)`:

```sql
WHERE o.ip_id = p_ip_id
  AND o.status = 'POSTED'
  AND o.operation_date >= p_period_start
  AND o.operation_date <= p_period_end
...
-- start balance:
  AND o.operation_date < p_period_start
```

- Both the period window and the start-balance anchor key on `operation_date`. Correct.
- Note for test authors: expense lines are reported as **positive magnitudes**
  per category (`materials`, `payroll`, `commissions`, `operating_expenses`);
  `ending_balance` carries the signed net.

## 3. `/operations/daily` uses `accrual_date` — VERIFIED (code correct, docstring stale)

`app/api/__init__.py` (~lines 598–605):

```sql
SELECT o.accrual_date::date AS day, ...
WHERE o.accrual_date IS NOT NULL
  AND o.accrual_date BETWEEN :start AND :end
  AND o.status = 'POSTED' ...
```

- Correct per P&L-series semantics.
- ⚠️ The endpoint docstring still says `day = date(operation_date)` — stale,
  contradicts the SQL. Cosmetic; flag for a doc-only fix.

## 4. `/operations/summary` uses `operation_date` — OPEN QUESTION (V2, unresolved)

`app/api/__init__.py` (~lines 800–803):

```python
if period_start is not None:
    stmt = stmt.where(Operation.operation_date >= period_start)
```

- The endpoint is a ledger-browse KPI (TD-016 E18, UI "Операции"): counts and
  sums of ledger rows by status/kind. By that contract `operation_date` is
  defensible.
- However its INCOME/EXPENSE totals read as P&L-like figures, and every other
  P&L surface (`pnl()`, `/operations/daily`, `v_ip_financial_summary`) keys on
  `accrual_date`. A cross-period accrual therefore appears in `/operations/summary`
  totals in a different month than in P&L — an inconsistency a user can see.
- This is the P&L-audit V2 finding; it is **not** proven fixed. Recommendation:
  explicit kind-aware semantics (P&L totals by `accrual_date`, cash totals by
  `operation_date`) or a documented decision that this endpoint is cash/ledger
  only. Needs owner/product input — do not guess.

## 5. `pay_payroll()` sets `accrual_date = payment_date` — VERIFIED, cross-period NOT supported

`app/services/__init__.py` (~lines 459–460):

```python
operation_date=payment_date,
# P&L: salary payment IS the economic event (calculate_payroll posts
# zero operations). Set accrual_date so it hits P&L (PNL_AUDIT V3).
accrual_date=payment_date,
```

- `calculate_payroll()` still posts zero ledger operations; the payroll expense
  is recognized only at payment time.
- Consequence: work performed in January but paid in February hits P&L in
  **February** — cross-period payroll accrual is not representable.
- This is a known limitation tied to TD-020 (payroll lifecycle, frozen pending
  owner). No code change made here; recorded as residual.

## 6. ETL sets `accrual_date = operation_date` — VERIFIED, per documented policy

`etl/import_excel.py` (~lines 317–319):

```python
# ETL: cash basis by default (accrual_date = operation_date).
# The Excel date semantic is undefined; backfill policy applies.
accrual_date=operation_date,
```

- Conservative default: an imported row hits both P&L and CF in the same
  period (cash basis), matching migration 0015's backfill policy.
- Correct under the current documented policy. Changing it would require the
  Excel date semantic to be defined (owner input).

## 7. `add_operation()` generic — DEFECT: explicit NULL indistinguishable from omitted

`app/services/__init__.py` (~lines 592–594):

```python
accrual_date=data.accrual_date if data.accrual_date is not None else data.operation_date,
```

- `OperationCreate.accrual_date` defaults to `None` (`app/schemas/__init__.py:74`),
  and the service cannot distinguish "field omitted" from "explicitly null".
- The schema comment promises: *"Set to None explicitly for pure
  payments/settlements"* — **the code does not honor this**: explicit `null`
  is silently replaced with `operation_date`, so a pure-payment row
  (`accrual_date IS NULL`) cannot be created through the public API today
  (only via raw SQL). That is the only way to get the NULL the strict
  `pnl()` filter relies on.
- Fix direction (not implemented here): use `model_fields_set`, or an explicit
  entry-role field, or the nullable `payment_date` dimension from §5.1.
  Requires a product decision — flagged, not changed.

## 8. Service-level regression test — WRITTEN AND PASSING

**File:** `tests/test_financial_regression_accrual_cf.py`

Unlike `test_accrual_accounting.py` / `test_accrual_matrix.py` (raw-SQL helpers
that mirror the logic), this test drives the **real** code paths:

- `app.services.add_operation()` — verifies `accrual_date` is stored as given
  (not defaulted to `operation_date`);
- `app.services.pnl()` — P&L by `accrual_date`;
- DB function `cash_flow()` — Cash Flow by `operation_date`.

Scenario (single row: `accrual_date=2026-01-15`, `operation_date=2026-02-10`):

| Assertion | Expected | Result |
|---|---|---|
| P&L Jan total_expense / net_profit | 500,000 / −500,000 | ✅ PASS |
| P&L Feb (payment must not duplicate P&L) | 0 | ✅ PASS |
| CF Jan materials / ending_balance | 0 / 0 | ✅ PASS |
| CF Feb materials / ending_balance | 500,000 / −500,000 | ✅ PASS |

Run: `pytest tests/test_financial_regression_accrual_cf.py` → **1 passed**
(ruff clean; the 13 pre-existing accrual tests still pass).

Note: for `EXPENSE_MATERIAL` the outflow appears on the `cash_flow()` **materials**
line (positive magnitude), not `operating_expenses` — first draft of the test
asserted the wrong column and failed; the product behavior was correct.

---

## Residual gaps (not changed — for parent/owner triage)

1. **Unpaid accrual pollutes Cash Flow (key residual).** `operation_date` is
   `NOT NULL` and Cash Flow keys on it, so an accrual with no payment date has
   no clean representation: setting `operation_date = accrual_date` puts it in
   CF (Convention B). `tests/test_accrual_matrix.py::test_04_expense_accrued_unpaid`
   currently **asserts** CF = −150,000 for an accrued-unpaid expense — that
   assertion encodes behavior the user explicitly rejected
   ("accrued 500,000, payment 0 → CF January = 0").
   The single-row convention (this test) works only when the payment date is
   known. A nullable `payment_date`/`cash_date` dimension is the likely fix;
   it needs an owner decision.
2. **Stale model comment.** `app/models/finance.py:236-237` says *"P&L aggregates
   by COALESCE(accrual_date, operation_date)"* — contradicts the strict
   `IS NOT NULL` semantics in `pnl()`. Comment-only fix.
3. **Stale `/operations/daily` docstring** ("day = date(operation_date)"). Doc-only fix.
4. **`/operations/summary` kind-aware date semantics** — open product question (V2).
5. **`add_operation()` explicit-NULL defect** — pure payments unrepresentable via API.
6. **Cross-period payroll accrual** — not supported; TD-020 owner decision pending.
