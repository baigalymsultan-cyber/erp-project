# Accrual/Payment Data Model Design (2026-10-03)

**Scope**: Design analysis only. No code changes. No reversal code touched (REVERSAL_Q1_Q7 blocked).

**Question**: Can `(accrual_date, payment_date)` as nullable columns on the `operations` table represent all required economic patterns, or is a settlement model needed?

**Answer (up front)**: The single-row two-date model is **proven insufficient** for 5 of 8 edge cases. Recommended: `accrual_date` on `operations` + separate payment rows + a dedicated settlement link table. The `operations` table remains the single financial source of truth; the link table is not a second ledger.

---

## 1. Book evidence: what the book actually does

From `docs/research/book_excel_full.txt` (lines 755–800):

The book's register has two date columns on the **same row**: «Дата начисления» (accrual date) and «Дата оплаты» (payment date, renamed from «Дата операции»).

Entry rules (book, p. 28):
1. **Cash operations**: enter normally; «Дата начисления» stays **empty**.
2. **Accrual operations**: EITHER create a new row with «Дата начисления» filled (payment date empty), OR find the previously entered payment operation and fill in its «Дата начисления».
3. **Payment after accrual**: same — EITHER new row, OR fill «Дата оплаты» in the existing accrual row.
4. Example: Jan 31 salary accrued for 3 employees → Feb 15 salary paid → «можно занести новыми записями в реестр, а можно просто проставить даты выплаты зарплаты в уже имеющихся в реестре операциях начисления».

**Critical observation**: The book's model depends on **editing cells in place** (filling a date later into an existing row). This directly violates erp-kaspi's §16 immutability rule: POSTED operations cannot be UPDATED; corrections go through reversal/correction. In Excel you edit a cell; in this ERP you cannot.

The book also never addresses: multiple payments for one accrual, partial payments with remaining balance, or prepayments matched to later accruals. Its examples are all 1:1.

---

## 2. Current ERP reality

| Element | State |
|---|---|
| `operations.operation_date` | Single date per row; sole temporal dimension |
| `operations.related_operation_id` | Self-FK. Used for: (a) TRANSFER_OUT ↔ TRANSFER_IN pairing (`app/services/__init__.py:993-994`); (b) reversal/correction linking (schema comment). Already **overloaded with two meanings**. Constraint: no self-reference. |
| `transfers` table | `outbound_operation_id` + `inbound_operation_id` → operations. Paired legs. |
| Settlement / allocation concept | **Does not exist** (grep for settlement/allocation: zero hits). |
| Payroll → operations | `calculate_payroll()` posts **zero** operations (salaries hit P&L only at payment time, not at accrual). |
| Kaspi payout → operations | `post_kaspi_payout` creates **no** operations (per book analysis area 16). |
| Status values | POSTED / REVERSED / CORRECTED / DRAFT (accounting lifecycle, not treasury). |
| Immutability | §16: no UPDATE/DELETE on POSTED; corrections via reversal. |

---

## 3. Edge-case analysis vs the single-row (accrual_date, payment_date) model

Model under test: add `accrual_date DATE NULL` and `payment_date DATE NULL` to `operations`. One row = one amount = one accrual event + one payment event.

### Case 1: One accrual → multiple payments (500k expense, 3 installments of 200k/200k/100k)

**Verdict: FAIL.**

A single row has exactly one `payment_date` column. Three installments have three different payment dates (e.g., Feb 10, Mar 10, Apr 10). There is no way to store three dates in one DATE column.

**Concrete counterexample**:
```
Row: id=101, amount=-500000, accrual_date=2026-01-31, payment_date=???
```
Setting `payment_date=2026-02-10` silently drops the Mar and Apr installments. Setting it to the last date misstates when cash left. Any single value is wrong.

**What is required**: 1 accrual row + 3 payment rows, linked. Either (a) 3 rows with `related_operation_id → 101` (but see §4: the FK is already overloaded), or (b) a settlement link table with 3 entries and per-payment allocated amounts.

### Case 2: Partial payment (500k accrued, 300k paid, 200k outstanding)

**Verdict: FAIL — this is the exact anti-pattern the user forbade.**

```
Row: id=102, amount=-500000, accrual_date=2026-01-31, payment_date=2026-02-15
```

The row's `amount` is -500,000 (the accrual). The `payment_date` being non-NULL implies the row is paid. But only 300k was paid. The model has **no field for "amount paid"** — the single `amount` column cannot simultaneously mean "500k accrued" and "300k paid".

Any report reading `payment_date IS NOT NULL` as "settled" will treat this as fully paid — precisely the artificial «полностью оплачено» the task forbids. Computing `remaining = 200k` is impossible from this row alone.

**What is required**: the payment must be its own row (`amount=-300000, payment_date=2026-02-15`), linked to the accrual row. Remaining = `500000 − 300000 = 200000`, computed from linked rows, never stored as a mutable balance.

### Case 3: Prepayment (paid 500k on Jan 15, goods/services received Feb 20)

**Verdict: FAIL under ERP immutability; works only in Excel.**

The book's rule 2 allows: create the payment row on Jan 15 (`payment_date=2026-01-15`, `accrual_date=NULL`), then when the accrual event occurs on Feb 20, **go back and fill `accrual_date`** on the existing row.

In erp-kaspi, the Jan 15 row is POSTED and immutable (§16). Filling `accrual_date` later is an UPDATE — forbidden. The alternative (book's rule 2, first option: create a **new** accrual row on Feb 20) leaves two unlinked rows: a 500k payment with no accrual, and a 500k accrual with no payment. Double-counting in any naive report, and no machine-readable link between them.

**What is required**: payment row (Jan 15) + accrual row (Feb 20) + a link recording that this payment settles that accrual. The link must be created at accrual time without mutating the posted payment row.

### Case 4: Payment before accrual / cash advance (advance paid to supplier, invoice arrives later)

**Verdict: FAIL — same as Case 3.**

Structurally identical to prepayment: the cash event precedes the economic event. The single-row model forces a choice between (a) illegal UPDATE of a posted row, or (b) two orphan rows with no settlement relationship. An advance is additionally semantically distinct from a prepayment-for-specific-goods (it may be applied to *future, unknown* invoices), which the single-row model cannot express at all — there is no "unapplied payment" state.

**What is required**: payment row marked as advance/unapplied + later allocation link(s) to accrual row(s) when invoices arrive. This is inherently a many-to-many relationship (one advance can settle multiple invoices; one invoice can be settled by advance + later payment).

### Case 5: Refund of a paid operation (customer paid 100k, refunded 100k)

**Verdict: FAIL — refunds are separate economic events, not date fields.**

```
Row: id=103, amount=+100000 (sale), accrual_date=2026-01-10, payment_date=2026-01-10
Refund on 2026-02-05: ???
```

There is no "refund date" column, and stuffing refund semantics into `payment_date` (e.g., overwriting it) destroys the original payment fact and violates immutability. Setting `payment_date=NULL` to mean "refunded" is a silent semantic overload that no report can interpret correctly.

**What is required**: a separate compensating operation row (`amount=-100000`, its own dates), linked to the original via `related_operation_id` (this matches the column's documented reversal/correction purpose). Note: `reverse_operation()` itself is BLOCKED (REVERSAL_Q1_Q7) — this analysis does not propose changing it; the refund-as-compensating-row pattern is independent of the reversal mechanism.

### Case 6: Unpaid accrued expense (500k accrued Jan 31, no payment yet)

**Verdict: WORKS on the single-row model.**

```
Row: id=104, amount=-500000, accrual_date=2026-01-31, payment_date=NULL
```

P&L (January) picks it up via `accrual_date`. Cash flow ignores it (`payment_date IS NULL`). AR/AP aging computes days outstanding from `accrual_date`. This is the one case the single-row model handles cleanly — and it is also the most common.

### Case 7: Unpaid revenue (sale recognized Jan 20, cash not received)

**Verdict: WORKS on the single-row model.**

```
Row: id=105, amount=+300000, accrual_date=2026-01-20, payment_date=NULL
```

Symmetric to Case 6. P&L recognizes revenue in January; cash flow shows nothing; AR balance = 300k.

### Case 8: Payroll partial payment (10 employees accrued, 6 paid)

**Verdict: FAIL at current granularity; works only with per-employee rows.**

Current reality: `calculate_payroll()` posts **zero** operations. There is nothing to attach a `payment_date` to.

Even if payroll posted a single lump-sum accrual row (`amount=-2000000` for all 10 employees), paying 6 of 10 is a partial payment of an indivisible row — identical to Case 2's failure. The row cannot say *which* employees were paid.

**What is required**: per-employee (or per-payroll-line) accrual rows. Then partial payment = 6 rows get linked payment rows, 4 rows remain with `payment_date IS NULL`. This also matches the book's own example (p. 29: «начислила заработную плату троим своим сотрудникам» — the book accrues per employee in its register figure).

---

## 4. Can `related_operation_id` link accrual↔payment pairs?

**No — it is already overloaded and semantically wrong for this.**

Current uses:
1. **Transfer pairing**: `outbound.related_operation_id = inbound.id` AND `inbound.related_operation_id = outbound.id` (bidirectional). Semantics: "these two legs are one economic event."
2. **Reversal/correction linking**: "this row corrects that row." Semantics: "this row supersedes that row."

Adding a third meaning — "this payment settles that accrual" — creates a column where a single FK value could mean three different things. Any query joining through it would need external knowledge of *which* relationship it represents. There is no discriminator column, and adding one (`relation_type`) to a generic self-FK is a known anti-pattern that degrades into an untyped graph.

Additional structural problems:
- A single FK is **many-to-one** (many payments → one accrual). Case 4 (one advance → many invoices) needs **many-to-many**, which a single FK cannot express.
- There is no place for `allocated_amount` (Case 2: which 300k of the 500k?).
- The bidirectional transfer usage (both rows point at each other) vs unidirectional reversal usage already makes traversal direction ambiguous.

**Conclusion**: `related_operation_id` must keep its current meanings. Settlement linking needs its own structure.

---

## 5. Transfer implementation: how paired operations are represented

For reference (not to be changed):

- `transfers` table is the **parent record**: `from_ip_id`, `to_ip_id`, `amount` (always positive), `transfer_date`, plus `outbound_operation_id` and `inbound_operation_id` FKs to `operations`.
- The two operation legs are `TRANSFER_OUT` (negative amount, `affects_pnl=FALSE`) and `TRANSFER_IN` (positive amount, `affects_pnl=FALSE`), cross-linked via `related_operation_id` (bidirectional).
- §28 invariant: a transfer is never income/expense; enforced by `affects_pnl=FALSE` and exclusion from P&L views.
- Constraint `chk_transfers_posted_has_legs`: a POSTED transfer must have both legs.

**Relevance to accrual design**: the transfer pattern shows the codebase's established idiom — a **parent/domain table** (`transfers`) owns the economic meaning, while `operations` holds the immutable ledger legs. A settlement model should follow the same idiom: a `settlements` (or `payment_allocations`) table as the parent record linking accrual and payment legs, not a second ledger.

---

## 6. Recommended model

### 6.1 Columns on `operations` (one-time migration)

| Column | Type | Nullability | Semantics |
|---|---|---|---|
| `accrual_date` | DATE | NULL | The date the economic event (revenue earned / expense incurred) is recognized for **P&L**. NULL = pure cash movement with no P&L recognition in any period (e.g., transfer legs, advances not yet applied). |

**No `payment_date` column.** Rationale: every operation row already has `operation_date`, which *is* the payment date for cash-movement rows. A separate `payment_date` column would duplicate `operation_date` for payment rows and create the proven-broken single-row conflation for accrual rows. The book's two-column layout is an Excel convenience; in a relational ledger, "payment" is a row, not a column.

**P&L rule**: `pnl()` / `v_ip_financial_summary` aggregate by `accrual_date` (not `operation_date`); rows with `accrual_date IS NULL` are excluded from P&L (they are cash-only movements).

**Cash-flow rule**: `cash_flow()` aggregates by `operation_date` (unchanged); only rows representing actual cash movement (unchanged type filter).

**Invariant preserved**: transfer legs get `accrual_date = NULL` → they can never leak into P&L via the date change. A payment settling an already-accrued expense is a cash-only row (`accrual_date IS NULL`) → it cannot double-count P&L. Receipt of already-recognized revenue: same.

### 6.2 New table: settlement links (not a second ledger)

```sql
CREATE TABLE settlement_links (
    id                   BIGSERIAL PRIMARY KEY,
    ip_id                BIGINT NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    accrual_operation_id BIGINT NOT NULL REFERENCES operations (id) ON DELETE RESTRICT,
    payment_operation_id BIGINT NOT NULL REFERENCES operations (id) ON DELETE RESTRICT,
    allocated_amount     NUMERIC(15,2) NOT NULL CHECK (allocated_amount <> 0),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by           BIGINT NULL REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT chk_settlement_not_self CHECK (accrual_operation_id <> payment_operation_id),
    CONSTRAINT chk_settlement_same_ip CHECK (... both operations belong to ip_id ...),
    CONSTRAINT uq_settlement_pair UNIQUE (accrual_operation_id, payment_operation_id)
);
```

This table records **facts about settlement relationships**, not money. All monetary facts stay in `operations`. It is many-to-many by construction.

**Derived (never stored)**: `remaining = ABS(accrual.amount) − SUM(allocated_amount)` per accrual; `unapplied = ABS(payment.amount) − SUM(allocated_amount)` per payment. Stored balances would violate the ledger-immutability principle; computed balances are always consistent.

### 6.3 How each edge case is represented

| # | Case | Representation |
|---|---|---|
| 1 | 1 accrual → 3 payments | 1 accrual row (`accrual_date=Jan 31`) + 3 payment rows (`accrual_date=NULL`, own `operation_date`s) + 3 `settlement_links` rows with per-installment `allocated_amount`. |
| 2 | Partial payment 300/500k | Accrual row 500k + payment row 300k + 1 link (`allocated_amount=300000`). `remaining=200000` derived. No false "fully paid" state exists anywhere. |
| 3 | Prepayment | Payment row Jan 15 (`accrual_date=NULL`, unapplied) + accrual row Feb 20 + link created Feb 20. No UPDATE of the posted payment row. |
| 4 | Cash advance → N invoices | Payment row (advance, unapplied) + N accrual rows + N links. Many-to-many natively. |
| 5 | Refund | Separate compensating operation row (negative amount, own dates), linked to original via `related_operation_id` (its documented correction purpose). Optionally a settlement link if the refund itself is paid out in cash. |
| 6 | Unpaid accrued expense | 1 row: `accrual_date=Jan 31`, no payment row, no links. P&L hits January; cash flow untouched; AP aging from `accrual_date`. |
| 7 | Unpaid revenue | Symmetric to 6. AR balance derived. |
| 8 | Payroll partial (6/10 paid) | Per-employee accrual rows (requires payroll to post operations — currently posts zero; separate work item). 6 employees get payment rows + links; 4 remain open. |

### 6.4 What the model CAN represent

- Accrual-basis P&L and cash-basis cash flow from the same ledger, with no double-counting (the `accrual_date IS NULL` rule on payment rows is the guard).
- Full AR/AP lifecycle: original amount, paid amount, remaining amount, accrual date, due date (add `due_date DATE NULL` on accrual rows — GAP-6.2 requirement), payment dates, counterparty, IP, document link, aging, status — all derivable from `operations` + `settlement_links`.
- Prepayments, advances, partial and installment payments, overpayments (unapplied remainder), refunds as compensating rows.
- Audit trail: every settlement assertion is an immutable row with `created_by`/`created_at`; nothing is ever edited in place.

### 6.5 What the model CANNOT represent (explicit limits)

- **Retroactive re-dating of a posted row.** By design (§16). If a payment was posted to the wrong date, the correction path is reversal/correction, not UPDATE — same as every other ledger error.
- **Automatic matching.** The model stores *asserted* links, not *inferred* ones. Auto-matching payments to invoices (by amount/counterparty/date heuristics) is application logic on top, not part of the data model. Any auto-matcher must write explicit `settlement_links` rows so the assertion is auditable.
- **Currency conversion in settlement.** If a payment is in a different currency than the accrual, `allocated_amount` needs a currency qualifier or a conversion rule. The current schema is KZT-only (`amount` comment: "KZT"); multi-currency settlement is out of scope until the ledger itself supports it.
- **Netting/offset agreements** (mutual settlement between AR and AP with the same counterparty). Representable as a pair of compensating rows + links, but the *agreement* itself (the legal event) has no table. Flag as a future requirement if the business does barter/offset.
- **The book's "fill the date later" workflow.** Deliberately not supported — it contradicts §16. The two-row + link workflow replaces it with identical reporting outcomes.

### 6.6 Migration / backfill policy (for the implementer)

- `accrual_date` added as NULL-able; backfill: `accrual_date = operation_date` **only** for rows whose `operation_types.affects_pnl = TRUE` AND `status = 'POSTED'` (these rows were already counted in P&L by `operation_date`, so this preserves current report output exactly). Transfer legs (`affects_pnl=FALSE`) and non-POSTED rows keep `accrual_date = NULL`.
- This backfill is **not** a silent reconstruction of unknown dates — it is a semantics-preserving default documented here: "before the accrual model existed, the ledger was cash-basis; the backfill declares that historical P&L timing."
- `due_date`: NULL for all historical rows (unknown; do not invent).
- `settlement_links`: empty at migration. Historical accrual↔payment pairs are **not** reconstructed (the pairing information does not exist in the data). AR/AP opening balances for go-live come from owner-provided starting saldos (GAP-3B), not from inferred links.

---

## 7. Answers to the task's specific questions

1. **Can the single-row (accrual_date, payment_date) model represent all 8 cases?** No. Proven failures: Cases 1, 2 (structural — one DATE column cannot hold N dates; one amount column cannot hold two amounts), Cases 3, 4 (require UPDATE of posted rows, forbidden by §16), Case 5 (refund is an event, not a date), Case 8 (granularity). Cases 6, 7 work.
2. **Can `related_operation_id` link accrual↔payment?** No — already overloaded (transfer pairs + reversals), no discriminator, single FK cannot do many-to-many, no `allocated_amount`.
3. **How are transfers represented?** Parent `transfers` row + two `operations` legs (TRANSFER_OUT/TRANSFER_IN, `affects_pnl=FALSE`), bidirectionally cross-linked. The recommended settlement model follows this same parent-record idiom.
4. **Is a second accounting engine created?** No. `settlement_links` stores relationship assertions, not money. `operations` remains the sole ledger; P&L and cash flow both read it, differing only in date column and `accrual_date IS NULL` filter.
5. **Does the model allow "artificially fully paid"?** No — "paid" is never a flag. Paid amount is always `SUM(allocated_amount)` over links; remaining is derived. There is no state in the model that can falsely claim full settlement.
