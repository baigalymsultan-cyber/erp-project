# ACCRUAL_DATE Dependency Graph — Complete Touchpoint Inventory

**Date**: 2026-10-03
**Scope**: Every usage of `operations.operation_date` in ~/workspace/erp-kaspi (HEAD `b7437b6`)
**Method**: grep + manual inspection. Read-only analysis — reports what the code DOES.

---

## 1. Table Definition

**File**: `database/schema.sql:1490-1520`

| Column | Type | Nullable | Constraints |
|--------|------|----------|-------------|
| `operation_date` | `DATE` | **NOT NULL** | No CHECK on range |

**Other date columns on `operations`**: NONE. `created_at`/`updated_at` are `TIMESTAMPTZ` audit timestamps, not business dates.

**Indexes involving operation_date** (`database/schema.sql`):
- `idx_operations_date` on `(operation_date)` — line 1790
- `idx_operations_analytics` on `(ip_id, operation_date, type_id)` — line 1767
- `uq_operations_import_fingerprint` on `(ip_id, import_fingerprint)` — no date

**Audit triggers** (must be updated if adding `accrual_date`):
- Line 482: `fn_operations_audit` — lists `operation_date` in the changed-columns check
- Line 562: second audit trigger — same pattern

## 2. SQLAlchemy Model

**File**: `app/models/finance.py:232`

```python
operation_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
```

No other date field. No `accrual_date`. No `payment_date`.

**Model indexes** (`app/models/finance.py:164-165`):
- `idx_operations_analytics` on `("ip_id", "operation_date", "type_id")`
- `idx_operations_date` on `("operation_date",)`

## 3. Complete operation_date Usage Inventory by Layer

### 3a. Pydantic Schemas

| File:Line | Usage |
|-----------|-------|
| `app/schemas/__init__.py:71` | `OperationCreate.operation_date: date` — required input field |

### 3b. Services (operation creation)

| File:Line | Usage | Semantic |
|-----------|-------|----------|
| `app/services/__init__.py:457` | `operation_date=payment_date` in `pay_payroll()` (SALARY_PAYMENT) | **EXPLICIT: payment date** |
| `app/services/__init__.py:588` | `operation_date=data.operation_date` in `add_operation()` | Pass-through from API |
| `app/services/__init__.py:976` | `operation_date=data.transfer_date` (TRANSFER_OUT leg) | Transfer date |
| `app/services/__init__.py:986` | `operation_date=data.transfer_date` (TRANSFER_IN leg) | Transfer date |

**Only 4 `Operation()` construction sites in the entire codebase.**

### 3c. Services (reporting)

| File:Line | Usage |
|-----------|-------|
| `app/services/__init__.py:647-648` | `pnl()`: `Operation.operation_date >= period_start AND <= period_end` — **P&L period filter** |

### 3d. API Endpoints

| File:Line | Endpoint | Usage |
|-----------|----------|-------|
| `app/api/__init__.py:499` | `GET /operations` | Selects `operation_date` in list |
| `app/api/__init__.py:514,516` | `GET /operations` | Period filter on `operation_date` |
| `app/api/__init__.py:541` | `GET /operations` | Returns `"operation_date": isoformat()` |
| `app/api/__init__.py:567-587` | `GET /operations/daily` | Groups by `operation_date::date`, docstring: "day = date(operation_date)" |
| `app/api/__init__.py:785,787` | `GET /operations/summary` | Period filter on `operation_date` |
| `app/api/__init__.py:985` | `GET /operations/{op_id}` | Returns `"operation_date": isoformat()` |

### 3e. Database Functions/Views

| File:Line | Object | Usage |
|-----------|--------|-------|
| `database/schema.sql:2102` | `v_ip_financial_summary` | `date_trunc('month', o.operation_date)` — **P&L month bucket** |
| `database/schema.sql:2121` | `v_ip_financial_summary` | `GROUP BY date_trunc('month', o.operation_date)` |
| `database/schema.sql:2169-2170` | `cash_flow()` | Period filter: `o.operation_date >= p_period_start AND <= p_period_end` |
| `database/schema.sql:2177` | `cash_flow()` | Start balance: `o.operation_date < p_period_start` |
| `database/schema.sql:2195-2196` | `cash_flow()` | Article breakdown period filter |
| `database/schema.sql:2396` | `v_kaspi_reconciliation` | Selects `op.operation_date` |

### 3f. Repositories

| File:Line | Usage |
|-----------|-------|
| `app/repositories/__init__.py:48` | `operation_date=data.operation_date` — pass-through |

### 3g. ETL/Import

| File:Line | Usage |
|-----------|-------|
| `etl/import_excel.py:275` | `operation_date = parse_date(row["date"])` — from Excel "date" column |
| `etl/import_excel.py:286` | Fingerprint includes `operation_date.isoformat()` |
| `etl/import_excel.py:307` | `operation_date=operation_date` in Operation construction |

**The Excel "date" column semantic is UNDEFINED** — the mapping file determines what date the user puts there. Could be payment date, invoice date, or shipment date depending on the source spreadsheet.

### 3h. Frontend

| File:Line | Usage |
|-----------|-------|
| `frontend/reports.py:249` | `"operation_date": t["col_date"]` — displayed as "Date" column |

### 3i. Tests

14 test files reference `operation_date`. All use raw SQL INSERTs with explicit `operation_date` values. **If `accrual_date` is added as NULLABLE, all existing tests continue to pass unchanged.** If added as NOT NULL, every INSERT must be updated.

---

## 4. Per-Category Semantic Analysis

| # | Category | Where created | What `operation_date` MEANS today | Needs different accrual vs payment? | Ambiguity |
|---|----------|---------------|-----------------------------------|-------------------------------------|-----------|
| 1 | sale/revenue | `POST /operations` (manual) or ETL | **Ambiguous** — whatever date the user/API caller provides | **YES** — revenue recognized at sale/shipment, cash may arrive later (Kaspi payout T+1) | HIGH: no way to know if the date is accrual or payment |
| 2 | expense | `POST /operations` or ETL | **Ambiguous** — same as above | **YES** — expense incurred vs paid can differ (supplier invoices, utilities) | HIGH |
| 3 | payroll accrual | **NOT CREATED** — `calculate_payroll()` posts zero operations | N/A — no operation exists | **YES** — this is the missing accrual leg | CRITICAL: salaries hit P&L only at payment |
| 4 | payroll payment | `pay_payroll()` (`app/services/__init__.py:457`) | **EXPLICIT: payment_date** — `operation_date=payment_date` | **YES** — payment date ≠ accrual (pay period) | LOW (explicit), but no accrual leg to pair with |
| 5 | material purchase | `POST /operations` or ETL | **Ambiguous** | **YES** — goods received vs paid | HIGH |
| 6 | production cost | `POST /operations` or ETL | **Ambiguous** | **MAYBE** — depends on costing model | MEDIUM |
| 7 | Kaspi sale | **NOT CREATED** — orders don't create operations | N/A | **YES** — order date ≠ payout date | CRITICAL: revenue not in ledger at all |
| 8 | Kaspi payout | **NOT CREATED** — `post_kaspi_payout` creates only `KaspiPayout` row | N/A | **YES** — payout is cash receipt, not revenue | CRITICAL: cash inflow not in operations |
| 9 | bank transaction | `POST /operations` or ETL | **Ambiguous** | **LIKELY NO** — bank statement date is inherently a payment/cash date | LOW |
| 10 | transfer | `POST /transfers` (`app/services/__init__.py:976,986`) | **Transfer date** — movement between own accounts | **NO** — a transfer is a single atomic cash movement; accrual concept doesn't apply | NONE |
| 11 | refund/reversal | `POST /operations/{id}/reverse` (BLOCKED area — not inspected) | Unknown — reversal code frozen per REVERSAL_Q1_Q7 | **LIKELY YES** — refund has both economic event and cash movement | UNKNOWN (blocked) |
| 12 | adjustment | `POST /operations` (manual) | **Ambiguous** — depends on adjustment type | **MAYBE** | HIGH |

---

## 5. Implicit Assumptions Found (file:line)

### Assumes operation_date = PAYMENT date (cash basis)

| File:Line | Code | Evidence |
|-----------|------|----------|
| `app/services/__init__.py:457` | `operation_date=payment_date` | Variable is literally named `payment_date` |
| `database/schema.sql:2169-2170` | `cash_flow()` filters by `operation_date` | Cash flow MUST use payment dates; using `operation_date` assumes it IS the payment date |
| `app/api/__init__.py:581-587` | `/operations/daily` groups cash movements by `operation_date` | Daily cash report assumes payment semantics |

### Assumes operation_date = ACCRUAL date (P&L basis)

| File:Line | Code | Evidence |
|-----------|------|----------|
| `database/schema.sql:2102` | `v_ip_financial_summary` groups P&L by `date_trunc('month', o.operation_date)` | P&L SHOULD use accrual dates; using `operation_date` assumes it IS the accrual date |
| `app/services/__init__.py:647-648` | `pnl()` filters by `operation_date` | Same — P&L period filter assumes accrual semantics |

### The Contradiction

**`cash_flow()` and `pnl()`/`v_ip_financial_summary` use the SAME column for DIFFERENT semantic purposes.** This is only correct if every operation's accrual date == payment date (pure cash accounting). The moment any operation has accrual ≠ payment, one of these reports becomes wrong.

### Assumes operation_date is a single unambiguous event date

| File:Line | Code | Evidence |
|-----------|------|----------|
| `app/schemas/__init__.py:71` | Single `operation_date: date` field, no alternatives | API callers get ONE date slot |
| `etl/import_excel.py:275` | Single "date" column from Excel | Import provides ONE date |
| `database/schema.sql:482,562` | Audit triggers track `operation_date` changes | Immutability model assumes one business date |

---

## 6. What Would Break / Need Updates When Adding accrual_date

### Must change (functional)

1. **`v_ip_financial_summary`** (schema.sql:2102,2121) — P&L month bucket should use `accrual_date`
2. **`pnl()`** (app/services/__init__.py:647-648) — period filter should use `accrual_date`
3. **`cash_flow()`** (schema.sql:2169-2177,2195-2196) — should use `payment_date` (or keep `operation_date` if it is redefined as payment date)
4. **Audit triggers** (schema.sql:482,562) — add `accrual_date` to changed-columns check
5. **`OperationCreate` schema** (app/schemas/__init__.py:71) — add optional `accrual_date`
6. **`add_operation()`** (app/services/__init__.py:588) — accept and store `accrual_date`
7. **API responses** (app/api/__init__.py:541,985) — include `accrual_date` in output

### Should change (completeness)

8. **Payroll**: `pay_payroll()` sets `operation_date=payment_date` — should also set `accrual_date` (to pay period end or explicit). **BLOCKED by TD-020** — do not change payroll lifecycle semantics without owner decision.
9. **ETL**: `import_excel.py:275` — mapping should support separate accrual date column
10. **Frontend**: `frontend/reports.py:249` — display both dates

### Tests (no breakage if NULLABLE)

- All 14 test files use raw INSERTs without `accrual_date` — **they pass unchanged if the column is NULLABLE**
- New tests needed for: accrual-only, payment-only, cross-period, partial payment scenarios

### Indexes

- Consider adding `idx_operations_accrual_date` for P&L queries
- Existing `idx_operations_analytics (ip_id, operation_date, type_id)` may need a companion on `accrual_date`

---

## 7. Key Findings Summary

1. **Single date column, dual semantics**: `operation_date` is used as BOTH payment date (cash_flow) AND accrual date (P&L views). This is the core architectural flaw — correct only under pure cash accounting.

2. **Only 4 Operation construction sites**: `pay_payroll()` (explicit payment_date), `add_operation()` (pass-through), 2 transfer legs (transfer_date). Small surface area for changes.

3. **Payroll is the clearest case**: `operation_date=payment_date` is EXPLICIT at app/services/__init__.py:457, but there is NO corresponding accrual operation — `calculate_payroll()` posts zero operations. P&L recognizes salary expense at payment, not at accrual.

4. **Kaspi revenue/cash completely outside ledger**: Orders don't create operations; `post_kaspi_payout` creates only `KaspiPayout` rows. The two most important economic events for a Kaspi seller (sale and payout) bypass `operations` entirely.

5. **ETL date semantic is undefined**: The Excel "date" column could mean anything — the mapping doesn't distinguish.

6. **Adding NULLABLE accrual_date is non-breaking**: All existing tests use raw INSERTs; NULL default keeps them green. The breaking changes are in the REPORTING layer (which date each report uses), not the storage layer.

7. **The real work is semantic, not structural**: Adding the column is trivial. Deciding per-category which date goes where, backfilling history, and rewriting `cash_flow()` vs `pnl()` to use the right dates is the actual project.


> **2026-10-08 (R1)**: `app/repositories/` deleted — it was a decorative
> pass-through layer (0 production usages). Data access goes directly
> through services + SQLAlchemy. See Architecture Refactoring Phase notes.
