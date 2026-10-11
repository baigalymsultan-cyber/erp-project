# Kaspi Pay Statement Ingestion Architecture (official path only)

**Date:** 2026-10-04 · **Status:** DESIGN (no code changed)
**Basis:** `docs/KASPI_EVIDENCE_2026-10-04.md` — payout/commission/settlement APIs are
**NOT_FOUND** officially (EB-KASPI-1..4). The statement export is the **only official
channel** for settlement/commission data. **No fake Kaspi API is designed here.**
**Aligns with:** `docs/BANK_ADAPTER_DESIGN_2026-10-04.md` (generic bank pipeline).

---

## 0. Official facts this architecture is built on (no invention)

| # | Fact | Source |
|---|------|--------|
| F1 | Money for sales arrives on the Kaspi Pay account **net of commission**, online after each accepted payment | guide.kaspi.kz/partner/ru/red/returns/q369 (VERIFIED OFFICIAL) |
| F2 | The account statement shows **accepted payments and written-off commissions as two aggregate sums** | same (VERIFIED OFFICIAL) |
| F3 | Per-operation detail is visible in the Kaspi Pay mobile app ("Продажи за сегодня" → period → point of sale) — a screen, not an API | same (VERIFIED OFFICIAL) |
| F4 | Official export mechanism: «Экспорт из Kaspi Pay платежных документов, Выписок по Счету(-ам)… в форматах, определяемых на Kaspi Pay» (Excel/PDF/1C per partner materials) | kaspibank.kz partner agreement App. 2 §3.1.2 (VERIFIED OFFICIAL) |
| F5 | Commission tariff tables are published per category (reference only, not per-transaction truth) | guide.kaspi.kz conditions pages (VERIFIED OFFICIAL) |
| F6 | Shop API v2 (`X-Auth-Token`) covers orders/orderentries/products only — nothing about money movement | 2026-10-03 discovery, re-confirmed (VERIFIED OFFICIAL) |

**Consequence:** the statement is an **aggregate cash record**, not a per-order feed.
Per-order economic truth comes from Shop API orders; per-order commission detail
lives only in the mobile app (manual transcription). The architecture must never
derive per-order splits from aggregate statement lines.

---

## 1. Statement → normalized transaction mapping

### 1.1 What the export contains (known vs unknown)

| Semantic field | Officially known? | Notes |
|---|---|---|
| Account (Kaspi Pay current account) | KNOWN (F4 — «Выписки по Счету») | binds to `accounts` row with `type='KASPI_PAY'` |
| Statement period | KNOWN (F4) | from export parameters |
| Receipts aggregate (accepted payments) | KNOWN (F2 — «двумя общими суммами») | one aggregate credit sum |
| Commission write-off aggregate | KNOWN (F2) | one aggregate debit sum |
| Per-line date / amount / direction / description | **UNKNOWN** | Kaspi defines formats unilaterally (F4); no official column spec located. **Column names are NOT hardcoded.** |
| Per-line external reference / document number | **UNKNOWN** | may exist in Excel export; treated as optional |
| Value date vs operation date distinction | **UNKNOWN** | assume single date unless export proves otherwise |

### 1.2 Adapter strategy: layout-tolerant, column-mapped, never hardcoded

`KaspiPayStatementAdapter` (a `ProviderAdapter` per the bank design doc):

- **Input:** user-uploaded statement file (Excel/PDF/1C-exported). PDF is
  parse-best-effort; Excel/1C are the recommended inputs (stated in UI).
- **Column map:** user-provided (or saved per-account template), following the
  existing `etl/column_map.example.yaml` pattern. Required normalized fields:
  `date`, `amount`, `direction`, `description`. Optional: `doc_ref`, `value_date`,
  `counterparty`, `balance_after`.
- **Rule:** any line that cannot supply the four required fields → staged with
  `match_status='NEEDS_REVIEW'`, never auto-posted. Unknown columns are kept
  verbatim in `provider_extra` JSONB — nothing is silently dropped.
- **Aggregates:** the two official aggregate sums (F2) are parsed as
  **statement-level control totals**, not as postable lines: `SUM(lines) == receipts
  aggregate` (within 1.00 ₸ tolerance, same as reconciliation) else the whole
  import is flagged `NEEDS_REVIEW` (balance-control check, cf. bank design §5).
  The commission aggregate becomes **one** commission line (see §2).

This keeps the adapter honest: it adapts to whatever Kaspi exports, instead of
pretending an official column spec exists.

### 1.3 Normalized record (provider_code = 'KASPI_PAY')

Target: the generic `bank_statement_lines` staging (bank design §2.2), with:

- `provider_code='KASPI_PAY'`, `provider_account` = Kaspi Pay account identifier
  from the file, `account_id` = bound `accounts` row (`type='KASPI_PAY'`,
  `bank_code='KASPI'`, same `ip_id` — IP isolation enforced at bind time)
- `direction`: IN (receipts) / OUT (commission write-off, other debits)
- `amount`: always positive; sign carried by `direction`
- `value_date` ← statement date (UNKNOWN distinction → `entry_date` = same unless
  the export proves two dates)
- `document_number` ← doc_ref if present else NULL (never invented)
- `provider_extra`: `{commission_aggregate, receipts_aggregate, period_from,
  period_to, raw_columns…}` — full provenance, no loss

---

## 2. Commission handling — the double-count guard

### 2.1 The trap

F1: cash arrives **net** of commission. Two naive postings both double-count:

- (a) Post sale at **gross** from Shop API **and** post the net settlement as
  income → revenue counted 1×gross + 1×net.
- (b) Post commission from `orders.kaspi_commission` (Shop API field) **and**
  from the statement commission line → expense counted twice.

### 2.2 Posting rules (normative)

| Event | Operation | Amount | accrual_date | operation_date | Fingerprint |
|---|---|---|---|---|---|
| **SALE** (Shop API order / manual) | `SALE_KASPI_SHOP`, kind INCOME | **gross** (+) | sale/economic date | sale date | `kaspi-sale:{external_order_id}` (existing) |
| **SETTLEMENT** (statement receipt line / aggregate) | `SALE_KASPI_SHOP`, kind INCOME¹ | **net** (+) | **NULL** — pure cash, no P&L effect (D1 contract) | statement value date | `kaspi-settle:{file_hash}:{line_seq}` |
| **COMMISSION** (statement commission line) | `EXPENSE_KASPI_FEE`, kind EXPENSE | commission (−) | write-off date (= value date) | value date | `kaspi-comm:{file_hash}:{line_seq}` |

¹ Reuses `SALE_KASPI_SHOP` deliberately: the cash **settles the already-accrued
AR** of the sale. With `accrual_date=NULL` it is excluded from P&L (GAP-004
unconditional filter) but included in `v_counterparty_balances` as the settled
leg and in cash flow via `operation_date`. No new operation type is invented;
adding one (`KASPI_PAY_SETTLEMENT`) is a possible future refinement, not needed
now. Alternative considered and rejected: posting settlement as TRANSFER —
a transfer is movement between own accounts (§28), this is third-party cash.

### 2.3 Double-count guards (enforced, not documented)

1. **Commission is posted ONLY from statement lines or manual entry — never from
   `orders.kaspi_commission`.** The Shop API commission field is reconciliation
   input (`v_kaspi_reconciliation.commission_delta`), not a posting trigger.
   Enforced by: no code path from order import → `EXPENSE_KASPI_FEE`.
2. **Net-vs-gross:** `kaspi_payouts.net_amount` is **stored, not computed**
   (existing comment, OQ-004) and never posted as income. The income side is
   always the gross SALE; the cash side is always the net settlement with
   NULL accrual.
3. **Fingerprint uniqueness** (`uq_operations_import_fingerprint` + per-IP):
   re-importing the same statement line — or a commission aggregate from an
   overlapping statement — hits the unique constraint → counted as duplicate
   (F-ETL-008 pattern), never double-posted.
4. **Tariff tables (F5) are cross-check config only:** expected commission =
   tariff × category can flag `COMMISSION_MISMATCH` in reconciliation; it can
   never create a posting.

### 2.4 Entity chain (no mixing)

```
Kaspi Sale (Shop API order, gross, accrual date)
  → Kaspi Settlement (statement credit, NET, operation/value date, accrual NULL)
  → Commission (statement debit line, SEPARATE EXPENSE_KASPI_FEE op)
  → Account (accounts.type='KASPI_PAY', account_id on every op)
  → Reconciliation (internal classifier, §4)
  → Financial Operation (POSTED ledger rows)
```

Settlement date ≠ sale date (accrual vs cash basis, GAP-004). Commission is a
separate operation, never netted into the sale.

---

## 3. Linkage to existing tables — what changes, what stays

| Existing object | Change |
|---|---|
| `raw_kaspi_payloads` | **STAYS** — JSONB API responses (Shop API) + manual payout provenance. Statement **files** do NOT go here (see below). |
| `raw_bank_statements` (new, bank design §2.1) | **NEW home for statement files**: BYTEA content, `file_hash`, `provider_code='KASPI_PAY'`, FK to `import_batches`. Rationale (agreed with bank design): Kaspi table stores JSONB API payloads with `api_type IN ('SHOP','PAY')` and `payload_hash` dedup; bank files are binary/text with file-level dedup and no `request_type` semantics. Reuse would corrupt both contracts. |
| `bank_statement_lines` (new, bank design §2.2) | **NEW staging** for normalized Kaspi Pay lines (`provider_code='KASPI_PAY'`), immutable after import (DI-005 trigger pattern), `match_status` workflow. |
| `kaspi_payouts` | **Stays** as the payout registry. Now populated from **statement aggregates** (import-suggested, user-confirmed) or manual entry — never from an API. `external_payout_id` ← statement reference when present, else NULL (`uq_kaspi_payouts_external` is per-IP; NULLs don't collide). `operation_id` ← the settlement operation. `status` lifecycle (`EXPECTED→RECEIVED→RECONCILED…`) unchanged. |
| `kaspi_payout_items` (`KaspiPayoutItem`) | **Manual entry path stays** (`POST /kaspi/payouts/{id}/items`, EB-KASPI-1). Statement import may **suggest** items by matching Shop API orders (same 8-class classifier), but per-order splits are **never auto-created from aggregate statement lines** — the statement has no per-order data (F2/F3). Suggestion → user confirm → insert. |
| `v_kaspi_reconciliation` | **UNCHANGED** — joins orders ↔ payout_items ↔ payouts ↔ operations. Statement import feeds it indirectly (payouts + settlement ops). The new settlement ops (accrual NULL) appear via `p.operation_id`; the view needs no modification. |
| `import_batches` | **Reused as-is**: `source_type='KASPI_PAY'` already in the CHECK constraint. `source_hash` = file SHA-256. |
| `operations` | **Reused as-is**: `import_batch_id`, `import_fingerprint` (unique), `account_id`, `raw_payload_id` (points at `raw_kaspi_payloads` only for API/manual provenance; statement ops point at `bank_statement_lines` via fingerprint + batch — no invented FK). |
| `reconcile_payout_full()` (8-class classifier) | **Reused**: fed by statement-derived payouts + Shop API orders. No changes needed for the statement path. |
| Shop API sync (`sync_log`, `app/integrations/kaspi/`) | **Untouched** — orders surface only, no money movement. |

**DEFERRED (explicitly not built):** a real Kaspi payout/commission API adapter —
blocked on EB-KASPI-1..4. The adapter interface slot exists in the bank design
(`ProviderAdapter` ABC); the Kaspi implementation is the statement adapter above,
not an API client.

---

## 4. Idempotency — file hash + line fingerprint

Four independent layers; any one of them alone prevents a double-post:

1. **File level.** `import_batches.source_hash = SHA-256(file bytes)`,
   `source_type='KASPI_PAY'`, per-IP. Re-upload of the identical file →
   detected before parsing → batch recorded as duplicate, **zero new rows**.
   Different export of the same period (different bytes) → proceeds to line level.
2. **Line level.** `bank_statement_lines.line_fingerprint = SHA-256(
   'KASPI_PAY' | ip_id | account_id | value_date | amount | direction |
   doc_ref | normalized(description) )`, `UNIQUE(ip_id, line_fingerprint)`.
   Overlapping date ranges across two exports share fingerprints → duplicates,
   not double lines. F-ETL-008 pattern: per-row savepoint + `IntegrityError` →
   `rows_duplicates++`, import continues.
3. **Operation level.** `operations.import_fingerprint` = the same line
   fingerprint (`uq_operations_import_fingerprint` exists); `external_id` =
   statement doc_ref when present (unique per ip_id+type_id, ADR-010).
   Posting is therefore idempotent even if staging is re-processed.
4. **Commission level.** `kaspi-comm:{file_hash[:16]}:{seq}` — the aggregate
   commission line is one fingerprint per file; a second export containing the
   same write-off collides → duplicate, never a second `EXPENSE_KASPI_FEE`.

**Concurrency & crash recovery** (same guarantees as F-ETL-008):

- Two simultaneous imports of the same file: both pass the file-hash check
  (race), then collide on `line_fingerprint` unique → loser's rows become
  duplicates; no double operations. Batch counters record the outcome.
- Crash mid-batch: batch stays `RUNNING`/`FAILED`; retry replays the file —
  already-inserted lines are duplicates by fingerprint → **idempotent resume**,
  no compensating cleanup needed.
- Partial aggregate: if the control-total check (§1.2) fails, the batch is
  marked `NEEDS_REVIEW` and **no operations are posted** (staging only) —
  fail-closed, never half-posted.

---

## 5. Test plan

### 5.1 Adapter / mapping (unit)

1. Column-map parsing: Excel fixture with known columns → normalized fields
   correct; unknown column names → preserved in `provider_extra`, not dropped.
2. Missing required column (`amount`) → line staged as `NEEDS_REVIEW`, zero
   operations posted.
3. Aggregate control totals: fixture statement where `SUM(lines) ≠ receipts
   aggregate` → batch `NEEDS_REVIEW`, fail-closed.

### 5.2 Posting semantics (integration, real PG)

4. Full chain: Shop order (gross 100 000, commission 6.4%) + statement
   (net 93 600 credit, 6 400 commission debit) → ledger has exactly:
   one `SALE_KASPI_SHOP` gross (accrual NOT NULL), one settlement net
   (accrual NULL), one `EXPENSE_KASPI_FEE` 6 400. P&L shows revenue 100 000,
   expense 6 400 — **no double revenue, no double commission**.
5. Double-count guard: order with `kaspi_commission` set + statement import →
   exactly one `EXPENSE_KASPI_FEE` (order field never posts).
6. Re-import of the identical file → `rows_duplicates == lines`, zero new
   operations, zero new payouts.
7. Overlapping exports (same period, different bytes, same economic lines) →
   shared fingerprints deduplicate; new genuinely-new lines post normally.

### 5.3 Concurrency

8. Two concurrent imports of the same file (threads, separate sessions) →
   exactly one set of operations; loser's rows counted as duplicates;
   no unique-violation crash (handled `IntegrityError` per F-ETL-008).
9. Crash simulation: kill after staging, before posting → retry completes with
   no duplicates.

### 5.4 Reconciliation linkage

10. Statement-derived payout (status RECEIVED, `operation_id` set) + matching
    Shop orders + manual payout items → `v_kaspi_reconciliation` verdict `OK`.
11. Commission mismatch (statement 6 400 vs tariff-expected 6 000) →
    `COMMISSION_MISMATCH` verdict, **no auto-posting of the difference**.
12. IP isolation: statement for IP-A never creates operations/payouts visible
    to IP-B; cross-IP `account_id` bind rejected.

### 5.5 Security (feeds Agent 7)

13. Malicious filename / path traversal in upload → sanitized, stored by hash.
14. Statement file containing another IP's account identifier → bind rejected,
    import refused (no cross-IP leakage via file content).
15. Raw file bytes retrievable only with same IP scope as the import batch.

---

## 6. What is explicitly NOT in this architecture

- Any Kaspi payout/commission/settlement **API client** (EB-KASPI-1..4, NOT_FOUND).
- Hardcoded Kaspi Pay export column names (UNKNOWN per §1.1).
- Per-order commission splits derived from aggregate statement data.
- Automatic `MATCHED` verdicts from amount equality alone (bank design §6 —
  reconciliation framework, separate agent).
- New `operation_types` (reuse with NULL accrual per §2.2; a dedicated
  settlement type is a future option, not a requirement).
- Valuation, overhead allocation, or any OQ-010/TD-024 semantics.

## 7. Implementation order (for the parent wave)

1. `raw_bank_statements` + `bank_statement_lines` tables (bank agent's DDL) —
   `provider_code='KASPI_PAY'` is one row-discriminator, no Kaspi-specific table.
2. `KaspiPayStatementAdapter`: column-map config + Excel parser + control totals.
3. Posting service: settlement (accrual NULL) + commission (`EXPENSE_KASPI_FEE`)
   with fingerprints from §4; suggestion (not auto-creation) of `kaspi_payouts`.
4. Manual payout-item entry API (EB-KASPI-1) — unchanged scope, fed by suggestions.
5. Tests §5 in order; concurrency tests before merge.
