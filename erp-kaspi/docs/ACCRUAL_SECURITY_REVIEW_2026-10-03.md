# Security & Data-Integrity Review: Proposed `accrual_date` Change (2026-10-03)

**Type**: Pre-implementation audit. Code does not exist yet; this reviews the PROPOSED change.
**Proposed change**: Add `accrual_date` (nullable DATE) to `operations` via Alembic migration. P&L aggregates by `accrual_date`, Cash Flow by `operation_date` (as payment date). Historical backfill: `accrual_date = operation_date` where semantics match, NULL otherwise. No changes to reversal code, payroll lifecycle, or Kaspi integration.

**Review method**: Read `database/schema.sql` (operations table, immutability triggers, audit trigger, cash_flow(), v_ip_financial_summary), `app/services/__init__.py` (pnl()), `app/api/__init__.py` (auth model), `etl/import_excel.py` (fingerprint), Alembic migration pattern.

---

## CRITICAL findings

### C-1: Immutability triggers do NOT cover `accrual_date` — silent P&L period manipulation

**Location**: `fn_protect_posted_operation()` (schema.sql:463) and `fn_protect_posted_ledger_operation()` (schema.sql:534).

Both triggers enforce financial-field immutability on POSTED operations via an explicit column list (`NEW.operation_date IS DISTINCT FROM OLD.operation_date`, etc.). If `accrual_date` is added as a column WITHOUT updating these triggers, it remains **mutable on POSTED operations**.

**Impact**: Anyone with direct DB access (or a future API endpoint) could UPDATE `accrual_date` on a POSTED operation, silently moving it between P&L periods. No reversal, no correction workflow, no exception. This breaks the FIN-OPS-0001 §7 immutability guarantee and enables period-shifting fraud (moving expenses into a different month to manipulate reported profit).

**Mitigation**: The migration MUST add `NEW.accrual_date IS DISTINCT FROM OLD.accrual_date` to BOTH triggers. Add a regression test: UPDATE accrual_date on POSTED operation → expect exception.

### C-2: Double-counting risk — accrual + payment both hitting P&L

**Location**: `pnl()` (app/services/__init__.py:600), `v_ip_financial_summary` (schema.sql:2098), `cash_flow()` (schema.sql:2140).

Currently P&L and Cash Flow both filter by `operation_date`. The plan switches P&L to `accrual_date` while Cash Flow stays on `operation_date` (as payment_date). The risk:

- If a payment operation is created with `type_id` = EXPENSE (current pattern: `SALARY_PAYMENT`, `EXPENSE_MATERIAL` are expense-type operations), and the accrual is ALSO an EXPENSE operation, **both hit P&L** unless the payment is typed as non-P&L (e.g., settlement/AP-clearing type with `affects_pnl = FALSE`).
- There is NO DB constraint preventing two EXPENSE operations for the same economic event. The `operation_types.affects_pnl` flag is the only guard, and it depends on correct type assignment in application code.

**Current code evidence**: `calculate_payroll()` posts zero operations (salaries hit P&L at payment via `SALARY_PAYMENT`). If accrual operations are added later without changing payment operation types, payroll would double-count.

**Mitigation**: 
1. Define explicit operation-type taxonomy: accrual types (`affects_pnl = TRUE`) vs settlement types (`affects_pnl = FALSE`).
2. Add a DB CHECK or trigger: if `related_operation_id` links a payment to an accrual, the payment's type MUST have `affects_pnl = FALSE`.
3. Regression test: accrual (EXPENSE) + payment (settlement) → P&L counts accrual once, Cash Flow counts payment once.

---

## HIGH findings

### H-1: NULL accrual_date fallback rule is undefined — P&L silently drops operations

The proposal says "NULL where unknown" but doesn't specify which date P&L uses when `accrual_date IS NULL`.

**Options**:
- (a) `COALESCE(accrual_date, operation_date)` — P&L falls back to payment date (cash-basis for old data)
- (b) Exclude NULL-accrual operations from P&L — P&L silently UNDERSTATES historical profit

**Impact**: If (b) is implemented (or if the SQL simply filters `accrual_date BETWEEN`), historical P&L will show lower income/expenses than before the migration. Users will see "profit disappeared" with no explanation. If (a), the fallback must be EXPLICIT and DOCUMENTED, otherwise future developers may "fix" it to (b).

**Mitigation**: Mandate `COALESCE(accrual_date, operation_date)` in ALL P&L queries (pnl(), v_ip_financial_summary, any future P&L). Document the fallback rule in the migration and in a COMMENT ON COLUMN. Add a test: operation with NULL accrual_date appears in P&L under operation_date.

### H-2: P&L and Cash Flow WILL diverge for historical data — user confusion risk

After migration with backfill `accrual_date = operation_date`, historical P&L and Cash Flow will match (same dates). But for NEW operations where accrual_date ≠ operation_date, the two reports will show different period totals for the same economic activity.

**This is CORRECT behavior** (it's the entire point of accrual accounting), but:
- The `/reports/cash-flow` endpoint and P&L views have no explanation of why they differ
- Users comparing "January P&L" vs "January Cash Flow" will see different numbers with no UI guidance
- The `v_ip_financial_summary` view groups by `operation_date` month; if switched to `accrual_date`, dashboards will shift

**Mitigation**: 
1. Add a report-level disclaimer or tooltip explaining the basis difference.
2. Consider a reconciliation view: P&L (accrual) vs Cash Flow (payment) with the timing-difference bridge.
3. Document in user-facing docs, not just code comments.

### H-3: Migration is NOT safely reversible for the backfill

**ADD COLUMN** itself is safe (PostgreSQL 11+ does it without table rewrite when no DEFAULT). But:
- The backfill (`UPDATE operations SET accrual_date = operation_date WHERE ...`) WILL take row locks and generate WAL. On a large `operations` table, this can block concurrent writes.
- If the migration fails AFTER the backfill but BEFORE completion, the downgrade (`DROP COLUMN`) will lose the backfilled data. Re-running upgrade will re-backfill, but any MANUAL corrections to accrual_date made between upgrade and downgrade are lost.
- There is no `down_revision` data-preservation strategy in the current Alembic pattern (they use raw `op.execute`).

**Mitigation**:
1. Split into two migrations: (1) ADD COLUMN (nullable, no backfill), (2) backfill in batches with `WHERE accrual_date IS NULL LIMIT n` to avoid long locks.
2. Document that downgrade loses manual accrual_date corrections.
3. Test migration on a production-sized dataset before deploy.

### H-4: Authorization — who can set/backdate accrual_date?

**Current model**: `POST /operations` requires `ACCOUNTANT` privilege (app/api/__init__.py:412). There is NO separate permission for "backdating" vs "current-date" operations.

**Risk**: `accrual_date` enables moving P&L between periods. An ACCOUNTANT could backdate an accrual to a closed period to manipulate reported results. There is no "period lock" concept in the codebase.

**Mitigation**:
1. Short-term: Document that accrual_date backdating is allowed for ACCOUNTANT+ (same as operation_date). This is consistent with current permissions.
2. Medium-term: Consider a period-lock table (TD-019 related) that blocks accrual_date in closed periods. This is a new feature, not required for the initial change.
3. At minimum: the audit log captures WHO changed it (see A-1 below).

---

## MEDIUM findings

### M-1: Audit trail captures accrual_date changes but not as a first-class event

**Location**: `fn_audit()` (schema.sql:187), `audit_log` table (schema.sql:1024).

The generic audit trigger records full OLD/NEW row as JSONB on UPDATE. Since `accrual_date` is a column on `operations`, changes WILL be captured in `new_data`/`old_data`. However:
- There is no column-level diff; you must compare JSONB to find what changed.
- The `action` will be generic `UPDATE`, not a semantic `ACCRUAL_DATE_CHANGE`.
- If the immutability trigger (C-1) is fixed, POSTED operations can't have accrual_date updated anyway, so audit becomes less critical. But DRAFT operations CAN be updated, and the audit log is the only record.

**Mitigation**: Acceptable as-is. The audit log captures the change; no additional work needed unless period-manipulation becomes a concern (then add a specific trigger).

### M-2: Import fingerprint does NOT include accrual_date

**Location**: `row_fingerprint()` (etl/import_excel.py:110).

The fingerprint is computed from the SOURCE ROW (Excel columns), not DB columns. Adding `accrual_date` to the DB doesn't affect it. BUT:
- If the ETL mapping is extended to read `accrual_date` from Excel, the fingerprint SHOULD include it. Otherwise, two rows differing only in accrual_date would have the same fingerprint, and the second would be skipped as a duplicate.
- Current mapping (`column_map.example.yaml`) doesn't have accrual_date, so this is a future risk, not immediate.

**Mitigation**: When ETL mapping adds `accrual_date`, update `row_fingerprint()` to include it. Add a code comment noting this dependency.

### M-3: Transfer pair integrity uses operation_date

**Location**: schema.sql:657-658 (`outbound.operation_date = t.transfer_date AND inbound.operation_date = t.transfer_date`).

Transfer legs are matched by `operation_date`. If `accrual_date` is added, transfers should NOT get different accrual_dates on each leg (transfers have `affects_pnl = FALSE`, so P&L doesn't care, but consistency matters).

**Mitigation**: Document that TRANSFER operations must have `accrual_date = operation_date` (or NULL). Add a CHECK constraint or trigger to enforce this.

### M-4: `v_ip_financial_summary` period grouping changes meaning

The view groups by `date_trunc('month', o.operation_date)`. If P&L switches to `accrual_date`, this view must change to `date_trunc('month', COALESCE(o.accrual_date, o.operation_date))`. 

**Risk**: Any dashboard or API consuming this view will see shifted monthly totals. This is expected but must be communicated.

**Mitigation**: Update the view in the same migration. Document the change in release notes.

---

## LOW findings

### L-1: ADD COLUMN lock behavior

PostgreSQL 11+: `ADD COLUMN ... DATE NULL` (no DEFAULT) does NOT rewrite the table. It takes an `ACCESS EXCLUSIVE` lock but only briefly (metadata change). Safe for large tables.

**No action needed** as long as no DEFAULT is specified. If a DEFAULT is added later, it WILL rewrite the table.

### L-2: Index on accrual_date

P&L queries filtering by `accrual_date` will need an index: `CREATE INDEX idx_operations_accrual_date ON operations (accrual_date)`. Without it, P&L queries will seq-scan.

**Mitigation**: Include in migration. Use `CONCURRENTLY` to avoid locking (note: can't run in transaction block; Alembic needs special handling).

### L-3: API schema exposure

The `OperationCreate` schema will need an optional `accrual_date` field. Ensure it's validated (not in future, or allow future for prepayments? — business decision).

---

## Summary table

| ID | Severity | Finding | Mitigation required before implementation? |
|----|----------|---------|-------------------------------------------|
| C-1 | CRITICAL | Immutability triggers don't cover accrual_date | YES — update both triggers in migration |
| C-2 | CRITICAL | Double-counting (accrual + payment both P&L) | YES — type taxonomy + DB guard |
| H-1 | HIGH | NULL fallback rule undefined | YES — mandate COALESCE, document |
| H-2 | HIGH | P&L/Cash Flow divergence confusion | Document + UI guidance |
| H-3 | HIGH | Migration reversibility (backfill loss) | Split migrations, document |
| H-4 | HIGH | Backdating authorization | Document; period-lock is future |
| M-1 | MEDIUM | Audit captures but not first-class | Acceptable as-is |
| M-2 | MEDIUM | Fingerprint excludes accrual_date | Update when ETL mapping changes |
| M-3 | MEDIUM | Transfer legs + accrual_date | CHECK constraint |
| M-4 | MEDIUM | v_ip_financial_summary grouping | Update view in migration |
| L-1 | LOW | ADD COLUMN locks | No action (no DEFAULT) |
| L-2 | LOW | Missing index | Add CONCURRENTLY |
| L-3 | LOW | API schema | Add optional field |

## Recommended pre-implementation checklist

1. [ ] Update `fn_protect_posted_operation()` to include `accrual_date` in immutability check
2. [ ] Update `fn_protect_posted_ledger_operation()` to include `accrual_date` in immutability check
3. [ ] Define `COALESCE(accrual_date, operation_date)` as the canonical P&L date expression
4. [ ] Define operation-type taxonomy: which types are accrual (P&L) vs settlement (non-P&L)
5. [ ] Add DB guard preventing settlement operations from having `affects_pnl = TRUE` when linked to accrual
6. [ ] Split migration: ADD COLUMN first, backfill second (batched)
7. [ ] Add `CONCURRENTLY` index on `accrual_date`
8. [ ] Update `v_ip_financial_summary` to use COALESCE date
9. [ ] Add CHECK: TRANSFER operations must have `accrual_date IS NULL OR accrual_date = operation_date`
10. [ ] Document NULL policy, backfill rule, and fallback in migration comments

**No new business decisions invented.** The H-4 period-lock and L-3 future-date validation are flagged as potential OWNER DECISIONS if the parent agent wants to pursue them.
