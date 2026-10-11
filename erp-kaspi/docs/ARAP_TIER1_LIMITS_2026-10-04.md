# AR/AP Tier 1 limits — mathematical proof of where `v_counterparty_balances` breaks

**Date**: 2026-10-04
**Analyzed**: `database/schema.sql` §8.6 `v_counterparty_balances` (HEAD `54253e2`), `docs/ARAP_MODEL_ANALYSIS_2026-10-03.md`
**Method**: read-only. Each scenario tested against the view's exact formula with concrete numbers. No code changed.

## The view's formula (exact)

```sql
balance =
    SUM(amount) FILTER (WHERE accrual_date IS NOT NULL)   -- "accruals"
  − SUM(amount) FILTER (WHERE accrual_date IS NULL)       -- "cash settlements"
GROUP BY (ip_id, counterparty_id), status='POSTED', counterparty_id IS NOT NULL
```

Sign convention: INCOME ops are positive, EXPENSE ops negative. A settlement leg
carries the same sign as its accrual leg (e.g. expense accrual −500, cash payment −500).
Positive balance ⇒ AR (counterparty owes us); negative ⇒ AP (we owe them).

**Classification mechanism**: the view treats `accrual_date IS NULL` as "pure cash
settlement". This is an *implicit convention*, not a guarded one — nothing in the
schema forces settlement legs to have NULL accrual_date, and nothing forces accrual
legs to have one set. `settlement_links` (schema §4.12) exists in schema + SQLAlchemy
model but is referenced by **zero** application code paths and **not by this view**.

---

## Scenario verdicts

### S1. Multiple obligations, one payment — ❌ FAIL (invoice-level)

Accruals (accrual_date set): `+100`, `+200`, `+300` → total_accrued = 600.
Settlement (accrual_date NULL): `+250` → total_settled = 250.
**Tier 1 answer: balance = 350. Aggregate is correct.**

Counterexample for the question Tier 1 *cannot* answer:
> "How much is still owed on obligation #2?"

Under FIFO: #1 fully paid (100), #2 partially paid (150 of 200) → remaining on #2 = 50.
Under LIFO: #3 partially paid (250 of 300) → remaining on #2 = 200.
Under pro-rata: each 58.3% paid → remaining on #2 = 83.3.
**Three defensible answers; Tier 1 cannot distinguish them.** Aggregation destroys
allocation information — this is information-theoretic, not a missing feature.

### S2. One payment allocated across 2 obligations — ❌ FAIL (allocation)

Same root cause as S1. The *sum* is right; *which* obligations were settled is
unknowable. Any report "paid invoices / open invoices" is impossible from Tier 1.

### S3. Partial payment (paid 300 of 500) — ✅ PASS (aggregate)

Accrual `+500`, settlement `+300` → balance = 200. Correct remaining total.
Caveat: with multiple obligations (S1), "200 remaining" cannot be attributed —
passes only in the single-obligation case.

### S4. Advance payment (paid before invoice) — ⚠️ PARTIAL (net correct, semantics lost)

Settlement `+400` on D1 (no accrual yet): balance = 0 − 400 = **−400**.
Accrual `+500` on D2: balance = 500 − 400 = **100**. Net position is correct.

What breaks:
1. The −400 interim state is *indistinguishable* from AP (trade payable).
   Per the view's own sign convention it means "we owe the counterparty" — true
   here (refund-or-deliver obligation), but the *nature* (customer advance vs
   supplier invoice) is lost. A counterparty that is both buyer and seller nets
   both directions into one number.
2. Aging/due-date of the later invoice is unaffected by the prepayment in any
   defined way — no allocation rule exists to apply the 400 against the 500.

### S5. Overpayment (paid 600 for 500 invoice) — ⚠️ PARTIAL (net correct, label wrong)

Accrual `+500`, settlement `+600` → balance = **−100**.
Mathematically consistent (we owe the customer 100 = refund liability).
But the view's comment says negative = AP. A −100 on a *customer* is not a trade
payable; it is a customer credit. The single signed number conflates:
customer advances (S4), overpayments (S5), and genuine supplier payables.

### S6. Negative balance interpretation — ❌ FAIL (gross vs net)

Counterexample — counterparty X is both customer and supplier:
- Sale accrual: `+1000` (AR leg)
- Purchase accrual: `−700` (AP leg)
- No payments yet.

**Tier 1: balance = 1000 − 700 = +300** ("net AR 300").
True position: **AR = 1000, AP = 700** (gross).
Any question about total AR exposure, total AP exposure, or AR/AP ratios gets a
**wrong answer** (300 vs 1000). Netting AR against AP for the same counterparty
is only valid with an explicit netting agreement; Tier 1 nets unconditionally.
The `counterparties` table has no customer/supplier role, so direction cannot
even be inferred from master data.

### S7. Aging (how old is the debt?) — ❌ FAIL (provably impossible)

Counterexample:
- Accrual `+100`, accrual_date = D−200
- Accrual `+9000`, accrual_date = D−5
- Settlement `+5000`, accrual_date NULL, operation_date = D−2

**Tier 1: balance = 9100 − 5000 = 4100.** Correct total.
Question: "How much of the 4100 is >90 days overdue?"
- Under FIFO: oldest 100 paid first → remaining = 4100, of which **0** is >90 days.
- Under LIFO: newest paid first → remaining includes the **100** from D−200.
Tier 1 cannot answer. Aging buckets (0–30 / 31–60 / 61–90 / 90+) require
per-obligation (accrual_date, remaining) pairs. **Aging is mathematically
impossible from aggregates.**

### S8. Due date — ❌ FAIL (no data source)

`due_date` exists **nowhere** in schema.sql, models, or ETL. Even `settlement_links`
cannot help: due dates are per-obligation attributes. "Overdue" is undefinable
without it. (Aging S7 needs only accrual_date per obligation; "overdue" additionally
needs due_date per obligation.)

### S9. Counterparty + IP isolation — ✅ PASS (with two caveats)

Grouping is by `(ip_id, counterparty_id)` — isolation is correct.

Caveat 1 — **transfer pollution (MEDIUM)**: the view does not filter
`operation_types.kind`. A TRANSFER_IN/OUT with `counterparty_id` set (no CHECK
constraint forbids it) would enter `total_settled` (accrual_date is NULL on
transfers) and corrupt the balance. Today transfers normally have NULL
counterparty_id (filtered by WHERE), so this is latent, not active.

Caveat 2 — **status assumption**: only `POSTED` is included. `CORRECTED` ops are
excluded — correct *iff* every correction is superseded by a new POSTED op (the
documented §16 semantics), but the view silently depends on that discipline.
`REVERSED` accruals are excluded while their settlement legs (still POSTED,
accrual_date NULL) remain → balance shifts by the orphaned payment. Economically
this equals "refund due", which is arguably right, but it is emergent behavior,
not a designed rule.

---

## Additional structural limits

- **No opening balances.** The book's formula is `opening + accruals − payments`;
  the view computes `0 + accruals − payments`. Correct only with complete history
  from day one (GAP-3B, `opening_balances` table — still missing).
- **`settlement_links` is unused.** The table + model exist (migration 0015) but
  no writer and no reader reference it. Tier 1 deliberately doesn't need it;
  Tier 2 does.
- **Implicit classification.** "accrual vs settlement" is inferred from
  `accrual_date IS NULL`, not from an explicit flag. A backfill or ETL bug that
  leaves accrual_date NULL on an accrual leg silently moves it from "accrued"
  to "settled", *reducing* the reported balance. A `CHECK` or explicit
  `accrual_kind` (ARAP doc option Q3-B) would make this fail loudly.

## Verdict matrix

| # | Scenario | Tier 1 verdict |
|---|---|---|
| S1 | Multiple obligations, 1 payment | ❌ aggregate ✓ / allocation ✗ |
| S2 | Payment across 2 obligations | ❌ allocation unknowable |
| S3 | Partial payment (single obligation) | ✅ |
| S4 | Advance payment | ⚠️ net ✓ / nature lost |
| S5 | Overpayment | ⚠️ net ✓ / label wrong |
| S6 | Gross AR vs AP (same counterparty) | ❌ nets unconditionally |
| S7 | Aging | ❌ mathematically impossible |
| S8 | Due date / overdue | ❌ no data source |
| S9 | Counterparty + IP isolation | ✅ (2 caveats) |

**Conclusion: Tier 1 is a correct *net position* indicator per (IP, counterparty)
and nothing more.** It cannot do invoice-level settlement, allocation, aging,
overdue, or gross AR/AP. These are not implementation gaps — S1/S2/S7 are
information-theoretic: the required data does not exist in the inputs.

---

## Tier 2 technical design (design only — NOT implemented)

Proven necessary by S1/S2/S6/S7/S8. Minimal design reusing existing structures:

### D1. Obligation identity — no new table needed
The accrual `operations` row **is** the obligation (has id, amount, accrual_date,
counterparty_id, ip_id, external_id). Add two columns to `operations`:
- `due_date DATE NULL` — per-obligation payment deadline (S8). NULL = no terms.
- `document_no VARCHAR(100) NULL` — invoice/act number for human matching
  (`external_id` exists but is the *source-system* id with uniqueness semantics;
  a separate human document number avoids overloading it).

### D2. Allocation — `settlement_links` already exists, needs writers
`settlement_links(accrual_operation_id, payment_operation_id, allocated_amount)`
is exactly the many-to-many allocation record S1/S2 require. Missing pieces:
- **Writers**: service function `allocate_payment(ip_id, payment_op_id,
  [(accrual_op_id, amount), ...])` with validation
  `SUM(allocated) <= payment.amount` and same `(ip_id, counterparty_id)` on both legs.
- **Sign rule**: `allocated_amount > 0` always; remaining on an obligation =
  `ABS(amount) − SUM(allocated_amount)` for its accrual leg (handles negative AP
  accruals without sign confusion).
- **Orphan guard**: CHECK or trigger preventing settlement_links rows where the
  accrual leg is REVERSED without a compensating link (or document the emergent
  "refund due" behavior from S9-caveat-2 as the designed rule).

### D3. Derived per-obligation view (new)
```sql
-- v_arap_obligations: one row per accrual leg with counterparty
SELECT o.id, o.ip_id, o.counterparty_id, o.amount AS original_amount,
       o.accrual_date, o.due_date, o.document_no,
       COALESCE(SUM(sl.allocated_amount),0) AS paid_amount,
       ABS(o.amount) - COALESCE(SUM(sl.allocated_amount),0) AS remaining_amount,
       CASE WHEN ABS(o.amount) - COALESCE(SUM(sl.allocated_amount),0) <= 0
            THEN 'PAID'
            WHEN COALESCE(SUM(sl.allocated_amount),0) > 0 THEN 'PARTIAL'
            ELSE 'OPEN' END AS settlement_status,
       CURRENT_DATE - o.accrual_date AS age_days,
       CASE WHEN o.due_date IS NOT NULL AND o.due_date < CURRENT_DATE
                 AND ABS(o.amount) - COALESCE(SUM(sl.allocated_amount),0) > 0
            THEN 'OVERDUE' END AS overdue_flag
FROM operations o
LEFT JOIN settlement_links sl ON sl.accrual_operation_id = o.id
WHERE o.accrual_date IS NOT NULL AND o.status='POSTED'
  AND o.counterparty_id IS NOT NULL
GROUP BY o.id;
```
Aging buckets, overdue lists, and per-invoice remaining are then trivial
aggregations over this view. Gross AR = SUM(remaining) over positive-amount legs;
gross AP = SUM(remaining) over negative-amount legs — fixing S6.

### D4. What Tier 2 does NOT need
- No separate `ar_invoices`/`ap_bills` tables: the accrual operation row already
  carries every attribute (D1 adds the two missing ones).
- No stored `remaining_amount`: always derived (same principle as 0015's
  "paid/remaining never stored").
- No changes to Tier 1 view: keep it as the fast net-position indicator.

### D5. Owner decisions required before implementing Tier 2
1. **Allocation policy**: FIFO default / manual / FIFO+manual-override
   (ARAP doc Q2 — narrowed to A/B/C already).
2. **Document numbering**: is `document_no` free text or a sequenced series per IP?
3. **Due-date source**: payment terms per counterparty (new `counterparties.payment_terms_days`?)
   or per-document manual entry?
4. Whether customer advances / overpayments get their own sub-ledger treatment
   or stay as negative balances (S4/S5 semantics).

None of D1–D4 is implemented in this analysis. No migration, no model change,
no API change was made.
