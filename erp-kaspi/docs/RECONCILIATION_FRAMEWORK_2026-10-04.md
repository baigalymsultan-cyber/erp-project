# Reconciliation Framework — provider-independent design

**Date:** 2026-10-04
**Status:** DESIGN (no code)
**Depends on:** `docs/BANK_ADAPTER_DESIGN_2026-10-04.md` (bank_statement_lines, match_status workflow),
`v_kaspi_reconciliation` (§44), `settlement_links` (DI-001..DI-005), `ReconciliationRule` (§44, OQ-004)

---

## 0. Definitions

- **External fact** — a `bank_statement_lines` row: what the bank says happened. Immutable (DI-005 trigger pattern); only workflow columns (`match_status`, `matched_*`) are mutable.
- **Ledger fact** — an `operations` row (POSTED): what the ERP books. Immutable per §16.
- **Reconciliation** — a *relationship assertion* between an external fact and ledger fact(s). Like `settlement_links`, it stores relationships, not money. Amounts are always derived, never stored redundantly.
- **Never auto-MATCH on amount alone.** Round sums (₸100,000.00) collide across counterparties, dates, and even directions. Amount is necessary but never sufficient.

---

## 1. Match candidate model — field priority

Fields are evaluated in strict priority order. A higher-priority hit is required before lower-priority fields are even consulted (except date window, which always applies as a filter).

| # | Field | Bank side | ERP side | Rationale |
|---|-------|-----------|----------|-----------|
| 1 | **External reference** | `document_number`, `bank_reference` | `operations.external_id`, transfer document refs | Bank-assigned identifier; collision-resistant. The only field that can identify a payment alone. |
| 2 | **Exact amount + direction** | `amount` + `direction` (IN/OUT) | `operations.amount` (sign per §27) | The financial fact. Direction prevents matching a ₸500k inflow against a ₸500k outflow. |
| 3 | **Account binding** | `provider_account` → `accounts.id` | `operations.account_id` | Money moved in a *specific* account. Cross-account amount matches are coincidences until proven otherwise (transfer-pair rule is the explicit exception). |
| 4 | **Date window** | `value_date` (± `date_window_days`) | `operations.operation_date` | Bank value date ≠ ERP operation date (weekends, processing lag, batch posting). Window absorbs lag without weakening identity. Default ±1 day; configurable. |
| 5 | **Counterparty** | `counterparty_iin` (exact) → normalized `counterparty_name` | `operations.counterparty_id` → IIN / name | KZ reality: IIN/BIN is the stable key; names vary in spelling. IIN match is strong; name match is weak (normalised, case/punctuation-insensitive). |
| 6 | **Purpose** | `payment_purpose`, `knp_code` | operation comment / type | Weakest. Used only as tiebreaker or confirmation, never as a sole criterion. KNP codes have one privileged use: commission detection (Rule R3). |
| 7 | **Provider identifiers** | `provider_extra` (RRN, ePay invoice id, …) | adapter-specific | Provider-scoped; documented per adapter, never assumed cross-provider. |

**Hard gates (all rules):** same `ip_id`; `operations.status = 'POSTED'`; the operation is not already CONFIRMED-matched to another line (partial unique index on `matched_operation_id`); the line itself is `UNMATCHED`.

---

## 2. Verdict taxonomy and state machine

Two orthogonal concepts — **workflow state** (where the line is in the process) and **verdict reason** (why the engine thinks what it thinks). The existing Kaspi vocabulary (`MATCHED`, `AMOUNT_MISMATCH`, … on `kaspi_payout_items`) is *discrepancy verdicts for marketplace business semantics* and is **not reused** here; the bank vocabulary is workflow states.

### 2.1 States

```
UNMATCHED ──(auto suggest)──▶ SUGGESTED ──(human confirm)──▶ CONFIRMED
    ▲                              │                              │
    │                              ├──(human reject)──────────────┘
    │                              │
    │                         (human ignore + reason)
    │                              ▼
    │                           IGNORED
    │                              │
    └────(human reopen + reason)────┘
```

| Transition | Actor | Guard |
|---|---|---|
| UNMATCHED → SUGGESTED | matcher (deterministic rules §3) | hard gates §1; verdict reason recorded; rule version recorded |
| SUGGESTED → CONFIRMED | human (ACCOUNTANT+) | one line ↔ one operation; sets `matched_operation_id`; audit row (who, when) |
| SUGGESTED → UNMATCHED | human (reject) | reason optional; suggestion discarded, line re-eligible |
| UNMATCHED → IGNORED | human | **reason required**, stored in `provider_extra.audit`; e.g. "personal expense of owner, out of scope" |
| IGNORED → UNMATCHED | human (reopen) | reason required |
| CONFIRMED → UNMATCHED | human (reopen) | reason required; used for corrections. **Does not delete history** — the confirm event stays in audit. |
| → CONFIRMED by machine | — | **forbidden**. No auto-confirm, ever. Suggestion is the strongest automatic action. |

DB enforcement (CHECK, per bank design doc §4): `SUGGESTED/CONFIRMED ⇒ matched_operation_id IS NOT NULL`; `UNMATCHED/IGNORED ⇒ matched_operation_id IS NULL`.

### 2.2 Verdicts

| Verdict | Meaning | Attached to |
|---|---|---|
| `EXACT_REFERENCE_MATCH` | Rule R1 fired | SUGGESTED |
| `TRANSFER_PAIR` | Rule R2 fired | SUGGESTED (proposes linkage to `transfers`, not to a single op) |
| `COMMISSION_SUGGESTION` | Rule R3 fired | SUGGESTED (proposes *creating* `EXPENSE_BANK_FEE`, not matching) |
| `SCORED_CANDIDATE` | Rule R4 fired, score ≥ threshold | SUGGESTED |
| `PARTIAL_MATCH` | §4 — close but not exact | SUGGESTED (human decides: confirm as-is, adjust, or reject) |
| `MISMATCH` | deterministic contradiction: reference matches but amount differs beyond tolerance, or direction contradicts | SUGGESTED is **blocked**; line stays UNMATCHED with the contradiction recorded. Human must resolve. |
| `NEEDS_REVIEW` | ambiguous: 0 strong candidates but >1 weak ones, or score below threshold | line stays UNMATCHED; surfaced in review queue with candidate list |
| `DUPLICATE_IMPORT` | fingerprint already seen | not a match state — the import layer drops it before matching (idempotency §7) |

---

## 3. Deterministic matcher v1

Runs per `(ip_id, provider_account)` scope. Idempotent: re-running never creates duplicate suggestions (suggestion = row update, guarded by state).

**R1 — exact reference.** `line.bank_reference = op.external_id` (or document_number match) AND `|line.amount| = |op.amount|` exactly AND `op.operation_date ∈ value_date ± date_window_days` AND `op.account_id = line.account_id` AND direction consistent AND hard gates ⇒ `SUGGESTED(EXACT_REFERENCE_MATCH)`.

**R2 — inter-account transfer pair.** Line A: OUT, line B: IN, `|A.amount| = |B.amount|` exactly, `|A.value_date − B.value_date| ≤ 1 day`, `A.provider_account ≠ B.provider_account`, same `ip_id`, neither line matched ⇒ both `SUGGESTED(TRANSFER_PAIR)` with a pairing proposal referencing the existing `transfers` entity. The transfer itself is **not** created by the matcher — `add_transfer()` remains the only writer (frozen transfer ledger). Human confirms the pair, then books the transfer normally, then confirms the match.

**R3 — bank commission.** `line.knp_code ∈ commission_knp_reference` (seed table, per-bank, extensible via migration) AND `line.direction = OUT` ⇒ `SUGGESTED(COMMISSION_SUGGESTION)`: proposal to *create* an `EXPENSE_BANK_FEE` operation (never to match an existing op — a commission line is its own economic event). Kaspi commissions arriving inside payout aggregates are **not** handled here (they belong to the Kaspi layer, §6).

**R4 — scored candidate (no reference).** All of: exact amount + direction + account + date window + (IIN exact OR normalized-name match) ⇒ score; `score ≥ suggest_threshold` ⇒ `SUGGESTED(SCORED_CANDIDATE)`; below threshold ⇒ `NEEDS_REVIEW` with ranked candidates. Score weights: amount 40, account 20, date proximity 15, counterparty 15, purpose keywords 10. Weights are config, defaults are the safe choice; tuning them is **B** (configurable infra), not owner.

**Explicit non-rules (things the matcher must NOT do):**
- amount-only match (any size) — forbidden by §0;
- date-only or purpose-only match — forbidden;
- matching a line to a DRAFT/CANCELLED/REVERSED operation — hard gate;
- matching across IPs — hard gate (P1 if violated);
- auto-CREATE of SALE/EXPENSE operations from a line — only `post_unmatched_as_operations()` with an explicit human-approved `PostingPolicy` may do that, and UNKNOWN/NEEDS_REVIEW lines are never auto-posted.

---

## 4. PARTIAL_MATCH — exact definition

`PARTIAL_MATCH` is a *verdict*, not a state: the line becomes `SUGGESTED(PARTIAL_MATCH)` and a human decides. It fires when identity is established (reference or R4-score) but exactly one dimension is off:

1. **Amount within tolerance:** `0 < |Δamount| ≤ amount_tolerance` (default ₸1.00, cf. `ReconciliationRule`). Cause class: bank fee netted in-line, rounding. Human confirms → CONFIRMED (the Δ is accepted, recorded in audit).
2. **Date drift:** reference + amount exact, but `|date Δ| > date_window_days` and `≤ max_drift_days` (default 7). Cause class: late settlement, batch posting. Human confirms → CONFIRMED.
3. **Split payment (1 line ↔ N ops):** `Σ|op.amount| = |line.amount|` exactly, all ops same account/direction/date-window. v1 does **not** auto-suggest (allocation across N ops is TD-024-adjacent semantics); verdict = `NEEDS_REVIEW` with the candidate set listed. Documented limitation, not invented allocation.

If **two** dimensions are off, or `|Δamount| > amount_tolerance` with a matching reference → `MISMATCH` (not partial): the contradiction is recorded and suggestion is blocked.

All thresholds live in one config object (pattern: `ReconciliationRule` — `amount_tolerance`, `date_tolerance_days`, plus `max_drift_days`, `suggest_threshold`). Changing a threshold is a B-class technical decision; inventing new match *semantics* is not.

---

## 5. Human workflow and audit trail

```
UNMATCHED queue (per IP, per account)
  ├─▶ SUGGESTED(EXACT_REFERENCE_MATCH) ── confirm ─▶ CONFIRMED
  ├─▶ SUGGESTED(TRANSFER_PAIR) ── confirm pair ─▶ book transfer ─▶ CONFIRMED ×2
  ├─▶ SUGGESTED(COMMISSION_SUGGESTION) ── approve ─▶ create EXPENSE_BANK_FEE ─▶ CONFIRMED
  ├─▶ SUGGESTED(PARTIAL_MATCH) ── accept Δ / adjust / reject
  ├─▶ NEEDS_REVIEW ── manual pick / ignore+reason / post-as-operation
  └─▶ MISMATCH ── investigate (never auto-resolves)
```

- **Confirm** requires privilege ACCOUNTANT+ and is IP-scoped (`ensure_ip_access`).
- **Ignore** requires a reason (free text, ≥10 chars), stored in `provider_extra.audit`; empty reason → 422.
- **Post-as-operation**: creates an `Operation` from the line with `import_fingerprint = line.line_fingerprint` — re-posting is idempotent via `uq_operations_import_fingerprint`. Classification follows §6 of the wave brief (TRANSFER ≠ EXPENSE, LOAN ≠ REVENUE, …); ambiguous → the created op is flagged `NEEDS_REVIEW` and does **not** auto-confirm the match.
- **Audit trail** (append-only table `reconciliation_events`): every transition records `line_id, from_state, to_state, verdict, rule_version, actor_user_id, reason, created_at`. Suggestions record the rule version so threshold changes are traceable. Nothing is ever deleted (DI-005 pattern).
- **Concurrency:** two matcher runs / two humans racing on one line → `SELECT … FOR UPDATE` on the line row; loser sees the new state and aborts. `matched_operation_id` has a partial unique index (`WHERE match_status = 'CONFIRMED'`) so one operation can never be CONFIRMED twice.

---

## 6. Relationship to settlement_links and v_kaspi_reconciliation

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 1 — EXTERNAL FACT (immutable)                          │
│  bank_statement_lines  ·  raw_bank_statements                │
│  kaspi_payouts / kaspi_payout_items (Kaspi's external fact) │
└──────────────────────┬──────────────────────────────────────┘
                       │  "does the ledger reflect the fact?"
┌──────────────────────▼──────────────────────────────────────┐
│ Layer 2 — GENERIC RECONCILIATION (this framework)            │
│  matcher v1 (R1–R4) · workflow states · reconciliation_events│
│  grain:  statement_line ↔ operation(s)                       │
└──────────────────────┬──────────────────────────────────────┘
                       │  CONFIRMED ⇒ matched_operation_id
┌──────────────────────▼──────────────────────────────────────┐
│ Layer 3 — LEDGER (immutable, §16)                            │
│  operations  ·  transfers (frozen)  ·  settlement_links      │
│  settlement_links: accrual↔payment ASSERTIONS (many-to-many), │
│  derived paid/remaining via SUM — a *downstream* consumer of  │
│  confirmed bank facts, not a replacement for them.           │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│ Layer 4 — BUSINESS SEMANTICS (provider-specific)              │
│  v_kaspi_reconciliation: Order ↔ Payout ↔ Operation triangle │
│  (§44 verdicts: OK / AMOUNT_MISMATCH / …). STAYS AS-IS.      │
│  A bank-CONFIRMED operation may *feed* the Kaspi triangle    │
│  (payout.operation_id), never the reverse.                   │
└─────────────────────────────────────────────────────────────┘
```

Rules of the layering:
1. The generic framework never interprets Kaspi business semantics (no commission/delivery/refund splitting — that is `reconcile_payout_full()`'s job).
2. `v_kaspi_reconciliation` never reads `bank_statement_lines`; it reads payouts, items, operations.
3. `settlement_links` may link an accrual to a payment operation that was CONFIRMED from a bank line — the bank framework does not create settlement_links itself (allocation semantics stay in their own layer, DI-001..005 guards apply).
4. A Kaspi commission line inside a *bank* statement (Kaspi Pay account statement) is a bank fact → R3/EXPENSE_BANK_FEE path; the same commission inside a *payout aggregate* is a Kaspi fact → payout path. Never double-count: the fingerprint namespace differs (`bank:` vs `kaspi-payout:`), and R3 skips lines whose `provider_extra` references an existing payout.

**Open (reversal freeze):** if a CONFIRMED operation is later reversed, the framework records a `reconciliation_events` row (`OPERATION_REVERSED`) but does **not** auto-transition the line — reversal semantics are owner-frozen (REVERSAL_Q1_Q7). The line shows in the review queue with the event attached.

---

## 7. Idempotency (matcher + import)

- **Import:** file-level (`file_hash` unique per IP) + line-level (`line_fingerprint` unique per IP) dedup, per F-ETL-008 pattern (per-row savepoint, `IntegrityError` → duplicate). Re-importing a file, overlapping date ranges, or retry-after-crash can never create a second line.
- **Matcher:** suggestion is a row *update* guarded by `match_status = 'UNMATCHED'` in the `WHERE` clause — re-running the matcher over already-SUGGESTED lines is a no-op. Two concurrent matcher runs: row lock, one wins.
- **Post-as-operation:** `import_fingerprint = line_fingerprint` + existing `uq_operations_import_fingerprint` → second post attempt raises the known duplicate path, never a second operation.
- **Crash between suggest and confirm:** state is in the DB; nothing in memory. The queue simply shows the line as SUGGESTED on restart.

---

## 8. Test plan (worked examples, exact numbers)

Setup per test: IP with two accounts (KZT-1, KZT-2); `ReconciliationRule(amount_tolerance=1.00, date_tolerance_days=3, max_drift_days=7)`.

| # | Scenario | Input | Expected |
|---|---|---|---|
| T1 | Exact reference | Line: OUT ₸500,000.00, ref `PP-2026-042`, value 2026-03-10, acc KZT-1. Op: `external_id='PP-2026-042'`, −500,000.00, operation_date 2026-03-11, account KZT-1, POSTED | `SUGGESTED(EXACT_REFERENCE_MATCH)` → human CONFIRMED; `matched_operation_id` set; event logged |
| T2 | Transfer pair | Line A: OUT ₸200,000.00 acc KZT-1 2026-03-10; Line B: IN ₸200,000.00 acc KZT-2 2026-03-10; no refs | Both `SUGGESTED(TRANSFER_PAIR)`; no transfer auto-created |
| T3 | Commission | Line: OUT ₸1,500.00, knp_code `123` (in commission reference), 2026-03-10 | `SUGGESTED(COMMISSION_SUGGESTION)`; approve → `EXPENSE_BANK_FEE` op created, fingerprint `bank:<fp>`; CONFIRMED |
| T4 | Amount alone never matches | Line: IN ₸1,000,000.00, no ref; three POSTED ops +1,000,000.00 same day, different counterparties | `NEEDS_REVIEW` with 3 candidates; **no** SUGGESTED |
| T5 | Partial — amount | Line: OUT ₸99,999.00 ref `INV-7`; op: −100,000.00 ref `INV-7` (Δ=1.00 ≤ tolerance) | `SUGGESTED(PARTIAL_MATCH)`; human accepts Δ → CONFIRMED, Δ in audit |
| T6 | Mismatch — amount | Same as T5 but op −99,998.00 (Δ=1.01 > tolerance) | `MISMATCH` recorded; suggestion **blocked**; line stays UNMATCHED |
| T7 | Partial — date drift | Line ref `D-9`, amount exact, value 2026-03-01; op operation_date 2026-03-06 (Δ=5d > window 3, ≤ max 7) | `SUGGESTED(PARTIAL_MATCH)` |
| T8 | Duplicate import | Same file imported twice | 2nd import: 0 new lines (fingerprint dedup); matcher untouched |
| T9 | Human reject | T1 suggestion rejected with reason | back to UNMATCHED; re-run matcher → SUGGESTED again (re-eligible); both events in audit |
| T10 | Cross-IP isolation | Line ip_id=1; identical op exists only under ip_id=2 | no suggestion; matcher scope is per-IP (P1 if violated) |
| T11 | Concurrent confirm | Two humans confirm same SUGGESTED line | one wins (row lock); loser gets 409 with current state |
| T12 | Split payment | Line IN ₸300,000.00; two ops +100,000.00 / +200,000.00 same account/day, no ref | `NEEDS_REVIEW` (1:N not auto-suggested in v1); candidate set listed |
| T13 | Reversed op | Op CONFIRMED to line is later reversed | `reconciliation_events` row `OPERATION_REVERSED`; line does **not** auto-unmatch (reversal freeze); shown in review queue |

---

## 9. What stays out (explicitly)

- **Owner decisions:** threshold *values* are B (configurable); new match *semantics* (e.g. 1:N auto-allocation, fuzzy name matching beyond normalisation) are C — not in v1.
- **Kaspi business semantics:** untouched (`v_kaspi_reconciliation`, `reconcile_payout_full()` stay as-is).
- **Valuation / allocation:** the framework asserts *relationships*; it never prices, allocates overhead, or revalues.
- **Auto-posting policy** (`post_unmatched_as_operations` classification SALE/EXPENSE/TRANSFER/…): needs the `PostingPolicy` from bank design doc §5.3 — referenced, not designed here.

---

*End of framework. Next: schema migration for `bank_statement_lines` + `reconciliation_events` (Agent 8 track), then matcher v1 service with the T1–T13 suite (TDD).*
