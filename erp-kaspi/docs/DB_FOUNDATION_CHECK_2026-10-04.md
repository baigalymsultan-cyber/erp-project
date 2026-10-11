# DB Foundation Check — 2026-10-04

Read-only integrity analysis for the bank-statement / inventory foundation wave.
Scope: `database/schema.sql`, `alembic/versions/`, `app/models/`. No code or schema changed.

## 1. `test_indexes_declared` — complete enumeration (verified 2026-10-04)

Current failure output (authoritative — test compares live DB vs SQLAlchemy metadata):

| Table | Missing index | Definition (schema.sql) | Verdict |
|---|---|---|---|
| `kaspi_payouts` | `idx_kaspi_payouts_operation` | `(operation_id) WHERE operation_id IS NOT NULL` | **DECLARE in model** — FK lookup for payout↔operation reconciliation (used at schema.sql:2332). Desirable. |
| `kaspi_payouts` | `idx_kaspi_payouts_raw_payload` | `(raw_payload_id) WHERE raw_payload_id IS NOT NULL` | **DECLARE in model** — FK lookup to raw payloads. Desirable. |
| `payroll_payments` | `idx_payroll_payments_operation` | `(operation_id) WHERE operation_id IS NOT NULL` | **DROP, do not declare** — REDUNDANT: `uq_payroll_payments_operation` UNIQUE on the same single column already provides an identical btree for all equality lookups (app/models/hr.py:444). |
| `payroll_summary` | `idx_payroll_summary_ip` | `(ip_id)` full | **DECLARE in model** — per-IP payroll queries; no covering constraint. Desirable. |
| `transfers` | `idx_transfers_from_account` | `(from_account_id) WHERE from_account_id IS NOT NULL` | **DECLARE in model** — account-based transfer lookup; composite FK `(from_account_id, from_ip_id)` does not create a single-column index; existing `idx_transfers_from` is on `(from_ip_id, transfer_date)` — different access path. Desirable. |
| `transfers` | `idx_transfers_to_account` | `(to_account_id) WHERE to_account_id IS NOT NULL` | **DECLARE in model** — same reasoning, to-side. Desirable. |
| `transfers` | `idx_transfers_outbound_op` | `(outbound_operation_id)` | **DROP, do not declare** — REDUNDANT: `uq_transfers_outbound` UNIQUE on the same column (app/models/finance.py). A unique btree serves every lookup the plain index would. |
| `transfers` | `idx_transfers_inbound_op` | `(inbound_operation_id)` | **DROP, do not declare** — REDUNDANT: `uq_transfers_inbound` UNIQUE on the same column. |

Net: **5 to declare** (with `postgresql_where=text(...)` for the partial ones, matching the existing model pattern e.g. `idx_kaspi_payouts_status`), **3 to drop** via a new migration (`DROP INDEX IF EXISTS`). The 3 redundant indexes cost write amplification and planner noise on every INSERT/UPDATE of `transfers`/`payroll_payments` — dropping is the correct fix, not declaring.

Origin: indexes added by `94f7a45` (P2-1/P2-2 DB hardening) and the Kaspi payout work — all ancestors of the accepted baseline `68aa080`. **Pre-existing**, not caused by any recent wave. The parent wave's fix is model/migration-side only; the test itself must not be touched.

## 2. FK coverage for planned tables

All target tables exist with `BIGSERIAL PRIMARY KEY`:

| Planned FK | Target | Status |
|---|---|---|
| `raw_bank_statements.import_batch_id` | `import_batches.id` | OK |
| `raw_bank_statements.account_id` / `bank_statement_lines.account_id` | `accounts.id` | OK |
| `bank_statement_lines.matched_operation_id` | `operations.id` | OK |
| `bank_statement_lines.statement_id` | `raw_bank_statements.id` (new) | new table — define in same migration |
| `stock_movements.material_id` | `materials.id` | OK |
| `stock_movements.order_id` | `orders.id` | OK (nullable) |
| `stock_movements.ip_id` | `ip_entities.id` | OK |
| any counterparty reference | `counterparties.id` | OK |

No missing PKs. Recommendation for the new migrations: FKs `ON DELETE RESTRICT` (financial-ledger convention, matches `operations`/`transfers`), per-IP uniqueness on fingerprints (`UNIQUE (ip_id, file_hash)` / `UNIQUE (ip_id, line_fingerprint)`), and partial FK indexes `WHERE <fk> IS NOT NULL` for nullable FKs — declared in the model from day one to avoid a repeat of §1.

## 3. `fn_settlement_links_immutable()` bypass — VERDICT: REAL, CONFIRMED

`database/schema.sql:1651-1666` (and identically in `alembic/versions/20261004_0016_settlement_links_integrity.py:122-134`):

```sql
-- Allow test purge via app.allow_financial_delete (same pattern as fn_forbid_delete)
IF coalesce(current_setting('app.allow_financial_delete', TRUE), 'off') = 'on' THEN
    RETURN OLD;
END IF;
```

The comment claims "same pattern as fn_forbid_delete" — **it is not**. `fn_forbid_delete()` (schema.sql:111-133) additionally gates on `pg_has_role(current_user, 'erp_migrator', 'member')`, with the TD-003 comment explaining why: *"a session-custom GUC can be SET by ANY role — PostgreSQL has no way to revoke SET on app.* parameters — so gating the hatch on the GUC alone was bypassable by the unprivileged application role (reproduced: erp_app SET the flag and physically DELETEd a posted operation)."*

Consequence: any session as `erp_app` can `SET app.allow_financial_delete = 'on'` and then UPDATE/DELETE `settlement_links` rows, silently breaking the DI-003 allocation cap and DI-001 same-IP guarantees. The test-purge path (conftest) runs as `erp_migrator`, so adding the membership check does not break tests.

**Fix (recommended, small, safe):** copy the exact `pg_has_role` gate from `fn_forbid_delete()` into `fn_settlement_links_immutable()`, in both `schema.sql` and a new Alembic migration (0018). Also fix the misleading comment. This is the only other `app.allow_financial_delete` consumer in the codebase (grep: only lines 118 and 1657) — no other trigger has the flaw.

Severity: HIGH for integrity (not data-loss — rows stay, but allocation accounting becomes untrustworthy). The bypass requires an authenticated DB session as `erp_app`, i.e. application-level compromise or a SQL-injection-class bug — defense-in-depth layer, but this layer was explicitly designed (TD-003) and is currently perforated for exactly one table.

## 4. Alembic — heads and chain

- `alembic heads` → single head: `0017_accounts_bank_code`. ✓
- Chain: `0015 → 0016 → 0017`, down_revisions verified in both files. ✓
- `downgrade()` present and symmetric in both (0016 drops the 2 triggers + 2 functions; 0017 drops the column). ✓
- No action needed. Note: if the §3 fix ships as 0018, it must `down_revision = "0017_accounts_bank_code"`.

## 5. Concurrent over-allocation in the DI-003 cap trigger — realistic assessment

`fn_settlement_links_integrity()` (schema.sql:1575-1643) checks the cap with a bare `SELECT SUM(allocated_amount) ... WHERE accrual_operation_id = NEW.accrual_operation_id` — **no lock**. Under the default `READ COMMITTED` isolation, two concurrent transactions inserting links against the same accrual op both read the same pre-existing SUM (neither sees the other's uncommitted row) and both pass; on commit the cap is exceeded. The prior wave's flag is **correct**.

- **Likelihood:** LOW today — allocation inserts are currently service-driven and effectively serialized; no concurrent allocation path is exercised in tests. It becomes REAL the moment reconciliation auto-links or two API workers allocate against the same accrual.
- **Impact:** MEDIUM — silent AR/AP over-allocation; the exact invariant DI-003 was built to enforce.
- **Fix (recommended, one line):** the trigger already does `SELECT ... FROM operations WHERE id = NEW.accrual_operation_id`; add `FOR UPDATE` to that SELECT. This takes a row lock on the parent operation for the transaction's duration, serializing concurrent link inserts per accrual op; under READ COMMITTED the second txn then re-reads the SUM including the first txn's committed row and correctly blocks. No isolation-level change, no advisory locks, no retry logic needed. (UPDATE path is moot — DI-005 makes rows immutable.)
- **Worth fixing now?** YES — it is a 1-line, strictly-strengthening change to a financial invariant, and the reconciliation work in this wave will create the concurrent path that triggers the race. Ship it in the same 0018 migration as §3.

## 6. Other schema/model drift

`test_models_match_schema.py` full run: 79 passed, 2 failed. Beyond `test_indexes_declared`:

- **`test_column_sets_match` FAILED — REAL DRIFT (G-BANK-1 incomplete):** `accounts.bank_code` exists in the database (migration 0017 applied) but is **absent from the SQLAlchemy model** (`app/models/reference.py:200`, class `Account` has no `bank_code`; the string appears nowhere in `app/*.py`). The previous wave flagged G-BANK-1 model parity as "not verified" — this test proves the gap. Fix: add `bank_code: Mapped[str | None] = mapped_column(String(20), nullable=True)` to `Account` (+ schema/API exposure per the G-BANK-1 follow-ups). Model-side only; no migration needed (column already exists).
- `test_foreign_keys_match`, `test_unique_constraints_match`, `test_check_constraints_declared`, `test_table_set_matches_database`, column types/nullability/defaults: all PASS. No other drift.

## Recommended fix order (for parent)

1. **0018 migration:** (a) add `erp_migrator` membership gate to `fn_settlement_links_immutable()` (§3, HIGH); (b) add `FOR UPDATE` to the accrual SELECT in `fn_settlement_links_integrity()` (§5). Both in `schema.sql` + migration. Targeted tests: bypass-repro (erp_app SET GUC → UPDATE still blocked) and a real 2-transaction concurrency test for the cap.
2. **Model declarations:** 5 indexes from §1 (`Index(...)` with `postgresql_where` where partial).
3. **0019 migration:** `DROP INDEX IF EXISTS` for the 3 redundant indexes (§1).
4. **Model:** add `Account.bank_code` (§6) — completes G-BANK-1 parity; then re-run `test_models_match_schema.py` to GREEN (except nothing — both failures are fixable without touching tests).
