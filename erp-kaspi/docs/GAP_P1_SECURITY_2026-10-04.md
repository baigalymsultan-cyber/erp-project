# P1 Security Gap Search — IDOR, Authorization, IP Isolation

**Date:** 2026-10-04
**Scope:** `app/api/__init__.py` (54 endpoints), `app/core/policy.py`, `app/core/security.py`
**Method:** Every endpoint traced for `ensure_ip_access` / `ensure_ip_access_or_all` / `ensure_employee_access`; object-level checks verified; 404/403 oracle consistency checked against P1-2 hardening precedent (`/operations/{op_id}`).

## Summary

| Severity | Count |
|----------|-------|
| P0 | 0 |
| P1 | 0 |
| P2 | 3 |
| Verified OK | 51 endpoints |

**No P0/P1 findings.** The authorization architecture is solid: centralized policy helpers, per-request role refresh, token versioning, password-change invalidation, DB-level audit triggers with secret masking, no PUT/PATCH/DELETE on ledger (append-only), idempotency scoped per-IP.

---

## SEC-001: 403/404 existence oracle on `POST /kaspi/reconcile/{payout_id}`

**Severity:** P2
**File:** `app/api/__init__.py:1773-1793`

**Evidence:**
```python
payout = session.get(KaspiPayout, payout_id)
if payout is not None:
    ensure_ip_access(session, principal, payout.ip_id)
```

A restricted principal probing `payout_id` values gets:
- `403` if the payout exists but belongs to another IP
- `404` (from `reconcile_payout_full` ValueError) if it doesn't exist

This is an existence oracle. The `/operations/{op_id}` endpoint was hardened against exactly this (P1-2, L981-985: scope the lookup to the allow-list so out-of-scope returns 404 indistinguishable from missing). This endpoint was not given the same treatment.

**Reproduction:**
1. Login as IP-restricted user (IP-A only).
2. `POST /kaspi/reconcile/99999` (non-existent) → 404.
3. `POST /kaspi/reconcile/<id-of-IP-B-payout>` → 403.
4. The 403/404 difference reveals which payout IDs exist.

**Business impact:** Low. Reveals only payout ID existence, no amounts or data (reconciliation itself is blocked by the 403). But inconsistent with the established P1-2 pattern.

**Proposed fix:** Scope the lookup like `/operations/{op_id}`:
```python
allowed = ensure_ip_access_or_all(session, principal)
stmt = select(KaspiPayout).where(KaspiPayout.id == payout_id)
if allowed is not None:
    stmt = stmt.where(KaspiPayout.ip_id.in_(allowed))
payout = session.scalar(stmt)
if payout is None:
    raise HTTPException(404, ...)
# ensure_ip_access still called for defense-in-depth
```

**Tests required:** New test: restricted user gets 404 (not 403) for another IP's payout ID; existing reconcile tests still pass.

**OWNER decision needed?** No. Pure technical consistency fix.

---

## SEC-002: 404/403 oracle + stale-IP check on payroll approve/pay

**Severity:** P2
**File:** `app/api/__init__.py:1626-1665`

**Evidence:**
```python
# post_payroll_approve (L1626)
summary = session.get(PayrollSummary, summary_id)
if summary is None:
    raise HTTPException(status.HTTP_404_NOT_FOUND, "payroll summary not found")
ensure_employee_access(session, principal, summary.employee_id)
# post_payroll_pay (L1640) — identical pattern
```

Two issues:

**(a) Existence oracle.** Same as SEC-001: 404 for missing vs 403 for out-of-scope summary IDs. Not hardened like `/operations/{op_id}`.

**(b) Stale IP attribution.** `PayrollSummary` has `employee_id` but **no `ip_id`** (verified in `app/models/hr.py`). `ensure_employee_access` checks the employee's *current* `assigned_ip_id`:

```python
# app/api/__init__.py:256-270
assigned = session.scalar(select(Employee.assigned_ip_id).where(Employee.id == employee_id))
if assigned is None or assigned not in allowed:
    raise 403
```

If an employee is reassigned from IP-A to IP-B after a summary was calculated for IP-A work, a principal with only IP-B access can now approve/pay the IP-A summary. The authorization check uses the wrong temporal IP.

**Reproduction (b):**
1. Employee E assigned to IP-A. Calculate payroll → summary S for E.
2. Reassign E to IP-B.
3. Login as user restricted to IP-B.
4. `POST /payroll/summaries/{S}/approve` → succeeds (checks current IP-B assignment).
5. The IP-A principal never approved IP-A payroll.

**Business impact:** Medium-low. Requires employee reassignment (uncommon) + a malicious or careless IP-B accountant. But payroll approval is a financial authorization — the wrong IP's money.

**Proposed fix:**
- For (a): scope the summary lookup to allowed IPs via employee join (consistent with P1-2).
- For (b): store `ip_id` on `PayrollSummary` at calculation time (denormalize from employee's assignment at that moment), and check `ensure_ip_access(session, principal, summary.ip_id)` instead of (or in addition to) the employee's current assignment. This is a schema change — needs migration.

**Tests required:**
- Restricted user gets 404 (not 403) for out-of-scope summary ID.
- Employee reassignment test: approval denied for new-IP-only principal after reassignment.

**OWNER decision needed?** Partially. The 404/403 hardening (a) is pure technical — no. The `ip_id` denormalization on `PayrollSummary` (b) changes the data model — but it's a technical correctness fix for an authorization gap, not a business semantic choice. Recommend: implement without owner, note in decision pack as informational.

---

## SEC-003: `POST /payroll/periods` — global periods creatable by IP-restricted ACCOUNTANT

**Severity:** P2
**File:** `app/api/__init__.py:1490-1505`

**Evidence:**
```python
def post_payroll_period(...):
    del principal  # <-- no IP check at all
    period = PayrollPeriod(start_date=..., end_date=..., ...)
```

`PayrollPeriod` has **no `ip_id`** (verified in `app/models/hr.py:220-240`). Periods are global. Any user with ACCOUNTANT privilege — including one restricted to a single IP — can create payroll periods that affect **all** IPs' payroll calculations. The `excl_payroll_periods_overlap` constraint means a malicious or mistaken period creation blocks payroll for every IP.

**Reproduction:**
1. Login as ACCOUNTANT restricted to IP-A.
2. `POST /payroll/periods` with overlapping dates → 500/422 (blocked by constraint, but the attempt is allowed).
3. `POST /payroll/periods` with valid non-overlapping dates → 201. Affects all IPs.

**Business impact:** Medium. Payroll periods are a shared financial calendar; unauthorized creation can disrupt payroll operations company-wide. However, ACCOUNTANT is already a privileged role.

**Proposed fix (options):**
- **A (minimal):** Require DIRECTOR/ADMIN for period creation (periods are company-wide by schema design).
- **B (schema):** Add `ip_id` to `payroll_periods` for per-IP calendars. Larger change; interacts with TD-020 (payroll lifecycle, frozen).

**Tests required:** Restricted ACCOUNTANT gets 403 on period creation (if option A).

**OWNER decision needed?** Yes — this is a business semantic question: are payroll periods company-wide or per-IP? The schema says company-wide, but the authorization model (IP-restricted ACCOUNTANTs exist) suggests the question wasn't considered. Recommend option A as safe default, with owner confirming whether per-IP periods are needed (which would be a TD-020-adjacent decision).

---

## Verified OK (no findings)

The following were explicitly checked and found correct:

| Area | Evidence |
|------|----------|
| `GET /operations/{op_id}` | P1-2 hardened: scoped lookup + `ensure_ip_access` (L975-1006) |
| `POST /operations` | `ensure_ip_access` on `data.ip_id`; idempotency replay scoped per-IP (L432-447) |
| `GET /operations`, `/operations/daily`, `/operations/summary` | Allow-list filtering (L505-507, L589, L784-786) |
| `POST /transfers` | Both `from_ip_id` and `to_ip_id` checked (L1405-1406) |
| `POST /orders` | `ensure_ip_access` on `data.ip_id` + order's IP (L1447, L1532) |
| `POST /payroll/work-entries` | `ensure_employee_access` + IP checks + order IP check (L1519-1532) |
| `/reports/work-entries` | Allow-list + `ensure_employee_access` for employee filter (L1176-1203) |
| `POST /payroll/calculate` | `ip_ids` passed to service; service filters employees and work entries (verified in `calculate_payroll` L288-296) |
| `GET /payroll/summary` | `ensure_employee_access` (L1594) |
| `GET /kaspi/payouts` | Allow-list enforced, empty list for no-access (L1754-1765) |
| `POST /kaspi/payouts` | `ensure_ip_access` + `assert_ip_exists` (L1723-1724) |
| `GET /employees` | Property-level auth: salary/phone only for HR/ADMIN/DIRECTOR (L2040-2041) |
| `GET /import-batches` | Allow-list scoped; NULL ip_id visible by explicit documented choice (L1365-1368) |
| `GET /audit-log` | DIRECTOR-only (L2139); DB trigger covers all tables including `users` |
| Auth endpoints | ADMIN-only for user management; roles re-read per request; token versioning; password-change invalidation |
| JWT | HS256, 32+ char secret from env, `decode_token` verifies signature+expiry |
| POSTED immutability | No PUT/PATCH/DELETE endpoints exist; mutations via POST actions guarded by DB triggers |
| Audit coverage | `fn_audit()` trigger on `users`, `user_roles`, all financial tables; `password_hash` masked; `app.current_user_id` set per-request |
| `/products`, `/materials` | Global catalogues (no `ip_id` in model); documented as global reference (E9/E10) |
| `/monitoring/summary` | Aggregated stats only, no per-IP financial data |
| SQL injection | All raw `text()` queries use bound parameters (verified in work-entries, payroll-due, cash-flow) |

## Not in scope (per task constraints)

- Transfer ledger: closed, not touched.
- TD-016: closed, not touched.
- Reversal (`POST /operations/{operation_id}/reverse`): frozen; authorization check present (`ensure_ip_access` L295) but semantics not evaluated.

## Recommended priority

1. **SEC-002(b)** — stale-IP payroll approval: real authorization gap on financial action. Implement `ip_id` denormalization or temporal check.
2. **SEC-003** — restrict period creation to DIRECTOR/ADMIN (option A) pending owner answer on per-IP periods.
3. **SEC-001 + SEC-002(a)** — 404/403 oracle hardening for consistency (low risk, easy fix).
