# P1 Data Integrity Gap Analysis — 2026-10-04

**Scope:** `database/schema.sql`, `alembic/versions/`, `app/models/`
**Method:** Read-only static analysis of constraints, triggers, FKs, uniqueness
**Status:** Findings below; no code changed

---

## Summary

| ID | Severity | Area | Status |
|----|----------|------|--------|
| DI-001 | HIGH | settlement_links: no same-IP enforcement | OPEN |
| DI-002 | HIGH | settlement_links: no operation role validation | OPEN |
| DI-003 | MEDIUM | settlement_links: no allocation sum cap | OPEN |
| DI-004 | MEDIUM | settlement_links: no POSTED status gate | OPEN |
| DI-005 | LOW | settlement_links: no immutability trigger | OPEN |
| — | — | All FKs have explicit ON DELETE | OK |
| — | — | POSTED immutability covers accrual_date (both fns) | OK |
| — | — | import_fingerprint per-IP unique | OK |
| — | — | amount != 0 CHECK | OK |

---

## DI-001: settlement_links — No Same-IP Enforcement

**Severity:** HIGH
**Table:** `settlement_links` (`database/schema.sql:1546-1569`)
**Evidence:**
- `ip_id` column exists with FK to `ip_entities` (line 1549)
- `accrual_operation_id` → `operations(id)` (line 1550)
- `payment_operation_id` → `operations(id)` (line 1551)
- **No CHECK or trigger** verifies `accrual_operation.ip_id = settlement_links.ip_id = payment_operation.ip_id`

**Impact:** A settlement link could connect operations from different IPs, breaking IP isolation at the financial matching layer. Cross-IP data leak via AR/AP derivation.

**Proposed fix:** DB trigger `trg_settlement_links_same_ip` BEFORE INSERT/UPDATE:
```sql
IF (SELECT ip_id FROM operations WHERE id = NEW.accrual_operation_id)
   <> NEW.ip_id OR
   (SELECT ip_id FROM operations WHERE id = NEW.payment_operation_id)
   <> NEW.ip_id THEN
    RAISE EXCEPTION 'settlement_links: cross-IP link blocked';
END IF;
```
Or composite FKs: `FOREIGN KEY (accrual_operation_id, ip_id) REFERENCES operations(id, ip_id)` — requires unique index on `operations(id, ip_id)`.

**Tests required:**
- Insert link with mismatched IPs → must fail
- Insert link with matching IPs → must succeed
- Update link to different IP → must fail

**OWNER needed?** No — technical integrity constraint.

---

## DI-002: settlement_links — No Operation Role Validation

**Severity:** HIGH
**Table:** `settlement_links` (`database/schema.sql:1546-1569`)
**Evidence:** No CHECK/trigger validates that:
- `accrual_operation_id` points to an accrual-type operation (e.g., `affects_pnl=TRUE` or expense/revenue kind)
- `payment_operation_id` points to a payment-type operation

A link could connect two payments, two accruals, or reversed roles.

**Impact:** Semantically invalid settlement assertions. AR/AP derived views (`v_counterparty_balances`) would compute wrong net positions. Silent financial misstatement.

**Proposed fix:** Trigger validation on INSERT:
- Accrual op: `operation_types.affects_pnl = TRUE` (or kind IN ('EXPENSE','INCOME'))
- Payment op: opposite direction or explicit payment type
- Exact role definitions need business confirmation (see OWNER note)

**Tests required:**
- Link payment→payment → must fail
- Link accrual→accrual → must fail
- Valid accrual→payment → must succeed

**OWNER needed?** PARTIAL — the role definitions (which operation types qualify as "accrual" vs "payment") touch TD-024 semantics. The mechanism (trigger) is technical. Recommend: implement trigger with configurable role check, OWNER confirms role mapping.

---

## DI-003: settlement_links — No Allocation Sum Cap

**Severity:** MEDIUM
**Table:** `settlement_links` (`database/schema.sql:1554`)
**Evidence:** Only `chk_settlement_links_amount_positive` (allocated_amount > 0). No constraint prevents:
```sql
SUM(allocated_amount) > ABS(accrual_operation.amount)
```
Multiple partial links could over-allocate beyond the accrual amount.

**Impact:** Over-payment assertions. Derived "remaining" goes negative. No DB-level protection against double-allocation of the same accrual.

**Proposed fix:** Trigger BEFORE INSERT/UPDATE on `settlement_links`:
```sql
IF (SELECT COALESCE(SUM(allocated_amount),0) FROM settlement_links
    WHERE accrual_operation_id = NEW.accrual_operation_id
    AND id <> COALESCE(NEW.id, -1)) + NEW.allocated_amount
   > ABS((SELECT amount FROM operations WHERE id = NEW.accrual_operation_id))
THEN RAISE EXCEPTION 'over-allocation blocked';
END IF;
```

**Tests required:**
- Allocate 60% then 50% of same accrual → second must fail
- Allocate 60% then 40% → must succeed
- Update allocation upward beyond cap → must fail

**OWNER needed?** No — mathematical integrity.

---

## DI-004: settlement_links — No POSTED Status Gate

**Severity:** MEDIUM
**Table:** `settlement_links` (`database/schema.sql:1546-1569`)
**Evidence:** No CHECK/trigger requires linked operations to have `status='POSTED'`. A link could reference DRAFT operations.

**Impact:** Settling unposted (draft) operations creates phantom AR/AP. If the draft is later modified/deleted, the link dangles semantically.

**Proposed fix:** Trigger BEFORE INSERT:
```sql
IF (SELECT status FROM operations WHERE id IN
    (NEW.accrual_operation_id, NEW.payment_operation_id))
   <> 'POSTED' THEN RAISE EXCEPTION ...;
END IF;
```
Note: `operations` has ON DELETE RESTRICT so hard-delete is blocked, but status changes DRAFT→POSTED→REVERSED are allowed.

**Tests required:**
- Link involving DRAFT operation → must fail
- Link involving REVERSED operation → must fail (or warn? — see OWNER)
- Both POSTED → must succeed

**OWNER needed?** PARTIAL — should REVERSED operations be linkable? Probably not, but reversal semantics are OWNER-blocked (REVERSAL Q1-Q7). Recommend: block DRAFT now (safe), defer REVERSED policy until reversal model decided.

---

## DI-005: settlement_links — No Immutability Trigger

**Severity:** LOW
**Table:** `settlement_links`
**Evidence:** No trigger prevents UPDATE/DELETE of settlement links. Financial assertions should be append-only (like `operations` which has `fn_forbid_delete`).

**Impact:** A link could be modified to change allocated_amount after the fact, rewriting settlement history. No audit trail of link changes.

**Proposed fix:** Apply `fn_forbid_delete()` trigger + immutability trigger on UPDATE (allow only INSERT). Corrections via new compensating link (consistent with reversal philosophy).

**Tests required:**
- DELETE FROM settlement_links → must fail
- UPDATE allocated_amount → must fail
- INSERT new link → must succeed

**OWNER needed?** No — consistent with existing §16 immutability principle.

---

## Verified OK (No Findings)

### FK Coverage
All foreign keys have explicit `ON DELETE` behaviors:
- Financial tables: `ON DELETE RESTRICT` (operations, transfers, settlement_links, work_entries, payroll_*)
- Provenance: `ON DELETE SET NULL` (raw_payload_id, import_batch_id, created_by, user_id)
- Line items: `ON DELETE CASCADE` (order_items, order_materials, order_status_history, kaspi_payout_items, user_roles)
- Deferred circular FKs in SECTION 5: `fk_orders_raw_payload`, `fk_raw_kaspi_payloads_sync_log`, `fk_kaspi_payouts_operation` — all present

No columns found that look like FKs but lack constraints. Composite FKs (`fk_operations_account_ip`, `fk_transfers_from_account_ip`) correctly enforce account-IP consistency.

### POSTED Immutability — accrual_date Covered
Both trigger functions include `accrual_date` in their protected column lists:
- `fn_protect_posted_operation()`: line 483 — `NEW.accrual_date IS DISTINCT FROM OLD.accrual_date`
- `fn_protect_posted_ledger_operation()`: line 564 — same

Migration 0015 correctly added these (verified in stabilization pass).

### Uniqueness
- `uq_operations_import_fingerprint` ON `(ip_id, import_fingerprint)` WHERE NOT NULL — schema.sql:1918
- `uq_work_entries_import_fingerprint` ON `(ip_id, import_fingerprint)` WHERE NOT NULL — schema.sql:1922
- `(ip_id, type_id, external_id)` unique per §41 — enforced via migration 0011
- `uq_payroll_summary_employee_period` — prevents double payroll
- `uq_payroll_payments_operation` — one ledger op backs one salary payment

### CHECK Constraints
- `chk_operations_amount_nonzero`: `amount <> 0` — present
- `chk_settlement_links_amount_positive`: `allocated_amount > 0` — present
- `chk_transfers_amount`: `amount > 0` — present
- `chk_payroll_payments_amount`: `amount > 0` — present
- Status enums: all tables have CHECK constraints on status columns

### Orphan Risks on DELETE
| Parent | Children | Behavior | Risk |
|--------|----------|----------|------|
| ip_entities | operations, transfers, etc. | RESTRICT | None — blocked |
| operation_types | operations | RESTRICT | None — blocked |
| counterparties | operations | RESTRICT | None — blocked |
| accounts | operations, transfers | RESTRICT (composite) | None — blocked |
| users | audit_log, created_by | SET NULL | Low — audit preserves, user ref nulled (documented) |
| orders | order_items/materials | CASCADE | None — line items correctly cascade |
| kaspi_payouts | kaspi_payout_items | CASCADE | None — items correctly cascade |

No orphan risks found. DELETE of financial parents is RESTRICT-blocked; the `fn_forbid_delete()` trigger additionally blocks physical DELETE of operations.

---

## Priority Order for Implementation

1. **DI-001** (HIGH) — same-IP trigger: straightforward, no business ambiguity
2. **DI-004** (MEDIUM) — POSTED gate for DRAFT: safe subset, defer REVERSED
3. **DI-003** (MEDIUM) — allocation cap trigger: mathematical, no ambiguity
4. **DI-005** (LOW) — immutability trigger: consistent with §16
5. **DI-002** (HIGH) — role validation: needs OWNER input on role definitions first

All fixes are new migrations (not modifications to 0015). None touch closed areas.
