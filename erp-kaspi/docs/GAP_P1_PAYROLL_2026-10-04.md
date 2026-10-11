# P1 Payroll Gap Search — Piecework Chain End-to-End

**Date:** 2026-10-04
**Scope:** `app/services/__init__.py` (payroll functions), `database/schema.sql` (payroll tables), `tests/test_payroll_flow.py`
**Constraint:** TD-020 frozen (owner decision). Document, don't fix payroll lifecycle.

## Chain Traced

```
employee → assigned IP → work entry → operation type → rate history →
quantity → piecework calc → payroll calculation → approval → payment →
financial operation → reports
```

---

## PAY-001: No un-approve function (error references non-existent action)

**Severity:** MEDIUM
**File:** `app/services/__init__.py:324-329`

**Evidence:**
```python
if summary is not None and summary.status == "APPROVED":
    raise ValueError(
        f"§36: summary {summary.id} is APPROVED; un-approve it before recalculating"
    )
```

But `grep -rn "def unapprove" app/` returns nothing. No `unapprove_payroll_summary` exists in services or API.

**Impact:** Once a summary is APPROVED, it can never be recalculated — even if the approval was a mistake. The only path forward is payment. The error message promises an "un-approve" action that doesn't exist.

**Proposed fix:** Add `unapprove_payroll_summary(session, summary_id)` (APPROVED → CALCULATED) with audit logging. Requires the approver's identity to be recorded (currently `approve_payroll_summary` doesn't take `approved_by`).

**Tests required:**
- APPROVED → un-approve → CALCULATED → recalculate works
- PAID cannot be un-approved
- Audit log records who un-approved

**OWNER decision?** No — technical gap. Implement.

---

## PAY-002: No partial payment support (service layer)

**Severity:** MEDIUM
**File:** `app/services/__init__.py:396-505` (`pay_payroll_summary`)

**Evidence:**
- `pay_payroll_summary` always pays `amount = total_calculated` (full amount)
- Sets `summary.status = "PAID"` unconditionally
- No `pay_partial` function exists
- DB trigger `fn_payroll_payment_no_overpay` allows `SUM(payments) <= total` (partial payments are DB-legal)
- But service layer has no way to record a partial payment

**Impact:** If a business pays salary in installments (common in KZ small business), the system cannot track it. The DB supports it, the service doesn't.

**Proposed fix:** Either:
- (A) Add `pay_payroll_partial(summary_id, amount, ...)` that inserts a `payroll_payments` row without marking PAID, OR
- (B) Document as "not supported" if business doesn't need it

**Tests required:**
- Partial payment 1/2 → summary still APPROVED (not PAID)
- Partial payment 2/2 → summary PAID
- Overpayment blocked by trigger

**OWNER decision?** YES — business question: does the business pay in installments?
- **OWNER-DECISION-PAY-002:** Is partial salary payment a required feature?
  - A: Yes, implement partial payment tracking
  - B: No, full payment only (current behavior is correct)
  - C: Defer

---

## PAY-003: Payroll periods are global, not per-IP

**Severity:** LOW (design limitation)
**File:** `database/schema.sql:926-938`

**Evidence:**
```sql
CREATE TABLE payroll_periods (
    id BIGSERIAL PRIMARY KEY,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    -- NO ip_id column
    CONSTRAINT uq_payroll_periods_range UNIQUE (start_date, end_date),
```

**Impact:** All IPs share the same payroll periods. If IP-A closes January but IP-B is still processing, they conflict. The `pay_payroll_summary` sets `period.status = "PAID"` globally — paying one employee marks the period PAID for all IPs.

**Current behavior:** `calculate_payroll` accepts `ip_ids` filter, but the period itself is global.

**Proposed fix:** Add `ip_id` to `payroll_periods` with unique `(ip_id, start_date, end_date)`. This is a schema change requiring migration.

**Tests required:**
- Two IPs can have different period statuses
- Paying IP-A employee doesn't close IP-B period

**OWNER decision?** YES — architectural:
- **OWNER-DECISION-PAY-003:** Should payroll periods be per-IP or global?
  - A: Per-IP (schema change, more flexible)
  - B: Global (current, simpler, but IP-B blocked by IP-A)
  - C: Defer

---

## PAY-004: Cross-period accrual (TD-020, documented)

**Severity:** KNOWN (frozen)
**File:** `app/services/__init__.py:452-456`

**Evidence:**
```python
operation = Operation(
    ...
    operation_date=payment_date,
    # P&L: salary payment IS the economic event (calculate_payroll posts
    # zero operations). Set accrual_date so it hits P&L (PNL_AUDIT V3).
    accrual_date=payment_date,
```

**Current behavior:** Work done in January, paid in February → P&L hit in February (not January). This is cash-basis, not accrual-basis.

**Impact:** Cross-period payroll misstates monthly P&L. January shows zero salary expense, February shows double.

**Status:** TD-020 frozen, owner decision pending. DO NOT FIX.

**Documented for:** Financial regression awareness.

---

## PAY-005: Rate freezing — VERIFIED WORKING

**Severity:** NONE (verified correct)
**Files:**
- `app/services/__init__.py:65-78` (`effective_rate`)
- `database/schema.sql:232-233, 250-259` (`fn_work_entry_freeze_rate`)

**Evidence:**
- `add_work_entry` calls `effective_rate(session, type_id, entry_date)` — gets rate in force on entry date
- DB trigger `fn_work_entry_freeze_rate` freezes rate at INSERT
- `fn_work_entry_protect_rate` prevents later modification
- `calculate_payroll` sums `WorkEntry.total_sum` (frozen, not recalculated)

**Historical rate changes:** If rate changes mid-period, entries before change keep old rate, entries after get new rate. Correct.

**No finding.** Rate history works as designed.

---

## PAY-006: Cross-IP leakage — VERIFIED PROTECTED (with edge case)

**Severity:** LOW (edge case documented)
**Files:**
- `app/services/__init__.py:111-124` (`add_work_entry` IP validation)
- `app/services/__init__.py:268-278` (`calculate_payroll` piecework query)

**Evidence (protected):**
```python
# add_work_entry:
if employee.assigned_ip_id != data.ip_id:
    raise ValueError("work entry ИП must match the employee's assigned ИП")

# calculate_payroll piecework:
.where(WorkEntry.ip_id == Employee.assigned_ip_id)
.where(or_(WorkEntry.order_id.is_(None), Order.ip_id == Employee.assigned_ip_id))
```

**Edge case:** Employee has single `assigned_ip_id` (no history). If employee transfers from IP-A to IP-B mid-period:
- Old work entries (IP-A) are excluded from calculation (filter uses CURRENT assigned_ip_id)
- Those entries remain CONFIRMED but never paid

**Impact:** LOW — employee transfer mid-period is rare, but the stranded entries are a data integrity issue.

**Proposed fix:** When employee's `assigned_ip_id` changes, either:
- (A) Migrate their CONFIRMED entries to new IP, OR
- (B) Block IP change while CONFIRMED entries exist, OR
- (C) Document as known limitation

**Tests required:**
- Employee IP change with pending CONFIRMED entries → handled per chosen option

**OWNER decision?** No — technical. Recommend (B) as safest.

---

## PAY-007: Overpayment guard — VERIFIED ENFORCED

**Severity:** NONE (verified correct)
**File:** `database/schema.sql:145-177, 2039-2041`

**Evidence:**
- Function `fn_payroll_payment_no_overpay()` checks `SUM(payments) + NEW.amount <= total_calculated`
- Trigger `trg_payroll_payments_no_overpay` installed as `BEFORE INSERT OR UPDATE`
- Test `test_payroll_overpayment_blocked_by_trigger` exists and passes

**No finding.** DB-level guard is active.

---

## PAY-008: Double-payment guard — VERIFIED ENFORCED

**Severity:** NONE (verified correct)
**File:** `app/services/__init__.py:424-478`

**Evidence:**
- `SELECT ... FOR UPDATE` on summary row (serializes racers)
- Status check: only APPROVED can be paid
- Partial unique index `uq_payroll_payments_operation` on `operation_id`
- `IntegrityError` caught and converted to "double payment blocked"
- Test `test_double_payment_and_status_guards_blocked` exists

**Idempotency:** No idempotency key on the API, but the DB constraints prevent double-payment even in races.

**No finding.** Double-payment is blocked at multiple layers.

---

## Summary

| ID | Severity | Status |
|----|----------|--------|
| PAY-001 | MEDIUM | No un-approve function — implement |
| PAY-002 | MEDIUM | No partial payment — OWNER DECISION |
| PAY-003 | LOW | Global periods — OWNER DECISION |
| PAY-004 | KNOWN | Cross-period accrual — TD-020 frozen |
| PAY-005 | NONE | Rate freezing verified OK |
| PAY-006 | LOW | IP transfer edge case — implement guard |
| PAY-007 | NONE | Overpayment guard verified OK |
| PAY-008 | NONE | Double-payment guard verified OK |

**Immediate actions (no owner needed):**
1. Implement `unapprove_payroll_summary` (PAY-001)
2. Add guard for employee IP change with pending entries (PAY-006)

**Owner decisions needed:**
- PAY-002: Partial payment support?
- PAY-003: Per-IP payroll periods?
