# AR/AP Verification — `GET /reports/counterparty-balances` (GAP-003)

**Date:** 2026-10-04
**Scope:** `v_counterparty_balances` (schema.sql §8.6) → `counterparty_balances()` service → `GET /reports/counterparty-balances` endpoint
**Method:** SQL logic review + live verification queries against `erp_test` (real PostgreSQL) + API-level isolation test with restricted user
**Constraint:** read-only; no code changed

---

## 1. View SQL logic review

```sql
total_accrued = SUM(amount) FILTER (WHERE accrual_date IS NOT NULL)   -- POSTED only
total_settled = SUM(amount) FILTER (WHERE accrual_date IS NULL)       -- POSTED only
balance       = total_accrued - total_settled
```

**Semantic:** accrual-side = economic events (invoice/expense recognized); settled-side = pure cash movements (accrual_date NULL). Positive balance = AR (counterparty owes us), negative = AP (we owe them).

**Is the dichotomy right?** For the standard flows — yes, because the ERP's sign convention (§27) forces accrual and its payments to share a sign: expense accruals and their cash-out payments are both negative; sale accruals and their cash-in payments are both positive. Under that invariant, `accrued − settled` nets correctly.

**Where it breaks — edge cases:**

| Case | Input | Result | Verdict |
|---|---|---|---|
| Full payment, 1 accrual | accrual −500k; payments −200k, −300k | balance 0 | ✓ correct |
| Partial payment | accrual −500k; payment −200k | balance −300k (AP) | ✓ correct |
| Overpayment | accrual −500k; payments −600k | balance **+100k** | ✓ defensible: advance to supplier is economically an asset (they owe us goods/refund). Label "AR" is loose but directionally right |
| Prepayment (no accrual) | payment −50k only | balance **+50k** | ✓ same as above — prepayment asset |
| **Refund after full payment (opposite-sign cash)** | accrual −500k; payment −500k; refund **+100k** (IN-type, accrual_date NULL) | balance **−100k (AP)** | ✗ **BUG** — see §6 |
| Refund netting against overpayment | accrual −500k; payments −600k; refund +100k | balance 0 | ✓ correct (netting happens to work) |
| DRAFT accrual −900k | — | excluded from view | ✓ correct (status='POSTED' filter) |
| Op with NULL counterparty | −700k | excluded | ✓ correct |
| 2nd IP, same counterparty | accrual −100k on IP2 | separate row, no contamination | ✓ correct |

## 2. Cross-IP leakage check — NO LEAK FOUND

Three layers, all verified:

1. **View:** emits `ip_id` per row; rows never mix IPs (verified: IP1/IP2 with same counterparty produce separate rows).
2. **Service:** `counterparty_balances(session, ip_ids)` — when `ip_ids` is not None, `WHERE ip_id = ANY(:ip_ids)`. When restricted user has no employee → `allowed=[]` → `ANY('{}')` → zero rows (not all rows).
3. **API:** `ensure_ip_access_or_all` → ADMIN/DIRECTOR get `None` (all IPs — by design, they are unrestricted); restricted roles get their employee's `assigned_ip_id`. Explicit `?ip_id=` of another IP → **403**.

**Live test (ACCOUNTANT assigned to IP1 only):**
- `GET /reports/counterparty-balances` → only IP1 rows ✓
- `?ip_id=<IP2>` → 403 ✓
- `?ip_id=<IP1>` → 200, only IP1 rows ✓

**Note (test gap, not a leak):** existing `tests/test_arap_endpoint.py` only covers the ADMIN path; the restricted path is verified here but not committed as a regression test.

## 3. Consistency matrix — worked example

Setup: IP1, counterparty "Supplier X": accrual −500k (2026-09-05, `accrual_date` set), payment −200k (2026-09-20, NULL), payment −300k (2026-10-02, NULL). All POSTED.

| | Endpoint returns | Should return |
|---|---|---|
| total_accrued | −500000.00 | −500000.00 ✓ |
| total_settled | −500000.00 | −500000.00 ✓ |
| balance | 0.00 | 0.00 ✓ |

`balance = accrued − settled` holds exactly (`NUMERIC(15,2)`).

AR-side symmetry (sale +800k, payment +300k) follows by sign symmetry of the FILTER sums; covered by construction. Existing committed test covers AP math (`−500k/−200k → −300k`).

## 4. settlement_links integration

**The view does not read `settlement_links` at all** — it is a pure operations aggregation.

**Should it?** For Tier-1 *net position*, no: `accrued − settled` is allocation-independent. settlement_links allocations only matter for Tier-2 questions ("which invoice is unpaid", "how much of accrual X is allocated") — explicitly deferred (TD-024, GAP-005). The DI-001..005 integrity triggers guarantee links can't corrupt the net (same-IP, POSTED gate, allocation cap).

**Documentation note:** this should be stated in the endpoint docstring — the endpoint answers "net position", not "allocated/remaining per invoice".

## 5. Why AR/AP ≠ bank balance ≠ cash flow ≠ P&L

These four numbers legitimately differ; the endpoint is not inconsistent with them:

- **AR/AP (this endpoint):** `SUM(accruals) − SUM(cash settlements)` by counterparty, **no date filter** (lifetime position), POSTED only, counterparty-scoped.
- **Bank balance** (`/reports/account-balances`): `SUM(all POSTED ops)` by `account_id` — includes ops **without** counterparty (salaries, transfers, fees), and transfers (which AR/AP excludes since they carry no counterparty). Different population.
- **Cash flow:** filters by `operation_date` within a **period**; an accrual-only op (accrual_date set, operation_date in another period) moves P&L but not CF.
- **P&L:** filters by `accrual_date` within a **period**, INCOME/EXPENSE kinds only; a pure-cash settlement (accrual_date NULL) moves CF/bank but not P&L.

Example from the worked case: P&L(Sept) = −500k expense; CF(Sept) = −700k (−500k accrual's operation_date −200k payment); bank = −500k total; AR/AP = 0 (fully settled). All four differ; all four are right.

## 6. Findings

### BUG-1 (MEDIUM): opposite-sign cash against a counterparty distorts balance

**Reproducer (SQL):**
```sql
-- IP with counterparty C: accrual -500k, fully paid -500k
INSERT INTO operations (ip_id, type_id, counterparty_id, amount, operation_date, accrual_date, status)
VALUES (:ip, :exp_type, :cp, -500000, '2026-09-05', '2026-09-05', 'POSTED'),
       (:ip, :exp_type, :cp, -500000, '2026-09-20', NULL,        'POSTED');
-- supplier refunds +100k (must be IN-direction type per §27 sign guard)
INSERT INTO operations (ip_id, type_id, counterparty_id, amount, operation_date, accrual_date, status)
VALUES (:ip, :sale_type, :cp, 100000, '2026-10-03', NULL, 'POSTED');
SELECT balance FROM v_counterparty_balances WHERE ip_id = :ip AND counterparty_id = :cp;
-- returns -100000.00 (AP: "we owe supplier 100k") — truth is 0 (nothing owed)
```

**Root cause:** `balance = accrued − settled` is only valid when settled ops share the accrual's direction. An opposite-sign cash movement nets into `total_settled` and fabricates a liability.

**Caveats:** booking the refund already requires an IN-direction type (SALE_*) against a supplier — a data-quality smell the §27 guard forces; there is no clean credit-note flow (returns/refunds flow missing — KASPI-007/F-ETL territory). So the bug needs unusual-but-plausible bookkeeping to trigger.

**Options:** (a) restrict the settled FILTER to ops whose type direction matches the counterparty's net accrual direction (needs per-row direction join — doable, no owner needed); (b) document that refunds must be booked as negative accruals (currently blocked by §27 — needs owner/TD-024 decision on credit-note semantics). Recommend (a) as the technical fix; (b) is the accounting-policy decision.

### DOC-1 (documentation gap)

Endpoint docstring says "Positive balance = AR (they owe us), negative = AP (we owe them)". For a **supplier** counterparty, a positive balance is an *advance/prepayment asset*, not classic AR. Suggest: "positive = net receivable position (customer owes us, or advance paid to supplier); negative = net payable".

### DOC-2 (documentation gap)

Nowhere stated that the endpoint ignores `settlement_links` (Tier-1 netting only) — add one line to the endpoint docstring.

### TEST-GAP (not a bug)

`tests/test_arap_endpoint.py` covers only ADMIN. Add a restricted-role test (ACCOUNTANT with assigned IP): sees only own IP; other IP → 403. Verified manually here; not committed.

---

## Summary

- View math: **correct** for accrual/payment flows, partial & multiple payments, over/underpayment, prepayments; DRAFT and NULL-counterparty correctly excluded.
- Cross-IP: **no leak** (view + service + API verified, including 403 on foreign `ip_id`).
- Consistency matrix: exact match on worked example.
- settlement_links: intentionally not used (Tier-1 netting); fine, but document it.
- **1 real bug (MEDIUM):** opposite-sign cash (refund) against a counterparty fabricates AP/AR — reproducer above.
- 2 documentation gaps, 1 test gap.
