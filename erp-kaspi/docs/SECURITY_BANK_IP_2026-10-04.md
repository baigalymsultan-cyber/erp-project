# Security Audit — Bank / Inventory / IP Isolation

**Date:** 2026-10-04
**Scope:** `~/workspace/erp-kaspi` — bank account isolation, IP isolation on new report surfaces, import replay/idempotency, raw-payload secrecy, SEC-002(b)/SEC-003 re-verification, financial-endpoint role matrix.
**Method:** read-only code inspection + one live DB reproducer (rolled back, nothing persisted).
**Prior report:** `docs/GAP_P1_SECURITY_2026-10-04.md` (0 P0, 0 P1, 3 P2 at the time).

## Findings

| ID | Severity | Area | Evidence (file:line) | Reproducer | Recommendation |
|----|----------|------|----------------------|------------|----------------|
| SEC-002(b) | **P1** — ESCALATED from P2 | Stale-IP payroll approval: after employee reassignment, another IP's restricted accountant can approve/pay the old IP's payroll | `app/api/__init__.py:1666-1712` — `post_payroll_approve`, `post_payroll_unapprove` (new, PAY-001), `post_payroll_pay` all do `session.get(PayrollSummary, …)` then `ensure_employee_access(session, principal, summary.employee_id)`; `app/core/policy.py:81-100` — check reads the employee's **current** `assigned_ip_id`; `app/models/hr.py:364` — `PayrollSummary` has **no** `ip_id` | **LIVE, 2026-10-04** (`/tmp/sec002b_repro.py`, fully rolled back): E1 at IP-A with CALCULATED summary; IP-B-restricted ACCOUNTANT → STEP1 `403` (correct); reassign E1→IP-B; STEP3 **no exception** — the IP-B accountant now passes the gate for the IP-A summary | Denormalize `ip_id` onto `payroll_summary` at calculate time (migration) and check `ensure_ip_access` on `summary.ip_id`. Technical correctness fix, no business semantics invented. |
| SEC-003 | **P1** — ESCALATED from P2 | IP-restricted ACCOUNTANT can create company-wide payroll periods | `app/api/__init__.py:1530-1541` — `post_payroll_period` does `del principal`: **zero** IP/authorization check; `PayrollPeriod` has no `ip_id` (global by schema) | Code-conclusive (no check exists to bypass). API reproducer: `POST /payroll/periods` as IP-restricted ACCOUNTANT → `201`; the period (and the `excl_payroll_periods_overlap` constraint) affects **all** IPs | Option A now: require DIRECTOR/ADMIN for period creation (periods are company-wide by schema; no owner needed for the restriction). Option B (per-IP periods) stays owner decision (TD-020-adjacent). |
| SEC-001 | P2 (unchanged) | 403/404 existence oracle on `POST /kaspi/reconcile/{payout_id}` | `app/api/__init__.py:1878-1885` — `session.get` then `ensure_ip_access`; missing→404 vs out-of-scope→403 | Described in prior report; code unchanged | Scope the lookup to the allow-list (prior report's fix, no owner needed). |
| SEC-002(a) | P2 (unchanged) | 403/404 oracle on payroll approve/pay/unapprove | `app/api/__init__.py:1666-1712` — same unscoped-`get` pattern | Same as SEC-001 | Same hardening; combine with the SEC-002(b) fix (one migration + lookup change). |
| RAW-1 | Low / note | `raw_kaspi_payloads` secrecy | **No** API endpoint references `raw_kaspi_payloads` (zero hits in `app/api/__init__.py`); `app/integrations/kaspi/client.py:28,64-86` — stores **response bodies only**, token travels via `X-Auth-Token` header, "payloads/secrets never logged (§71)" | No reproducer (no exposure path found) | Note: Shop order payloads contain customer PII in JSONB — DB-role access only, no API leak. Document retention; no action now. |
| IMP-1 | OK with arch note | Import replay / idempotency | `database/schema.sql:2019-2021` — `uq_operations_import_fingerprint` UNIQUE `(ip_id, import_fingerprint)` WHERE NOT NULL (per-IP scoped); `app/api/__init__.py:444-475` — replay lookup scoped per-IP (P1-1/M-1), race-safe savepoint on conflict (P2-1); ETL per-row flush `IntegrityError` → duplicate (F-ETL-008) | No reproducer (controls verified in code) | Planned `bank_statement_lines` (BANK_ADAPTER_DESIGN) must get the same per-IP fingerprint unique index — carry into implementation, not a current bug. |
| BANK-ISO-1 | OK (verified) | Transfer account binding | `app/services/__init__.py:1192-1198` — `account.ip_id` must equal the transfer's `from_ip_id`/`to_ip_id` and account active; `app/api/__init__.py:1410-1411` — both IPs checked against principal | — | None. `POST /operations` takes no `account_id` (API surface), ETL sets none — account binding only via verified transfer path. |
| RPT-ISO-1 | OK (verified) | New surfaces: `/reports/account-balances`, `/reports/counterparty-balances`, `/accounts`, `/pnl` | `app/api/__init__.py:1825-1875` — `ensure_ip_access_or_all` + explicit `ip_id` check, scope passed to service; service SQL filters `a.ip_id = ANY(:ip_ids)` / `ip_id = ANY(:ip_ids)` (`app/services/__init__.py:872-920`, `825-870`); `/accounts` `1288-1318` same pattern | — | None. |
| AUTH-1 | Info | Role matrix, financial endpoints | `ACCOUNTANT`: `POST /operations`, `/operations/{id}/reverse`, `/transfers`, `/payroll/calculate`, `/payroll/summaries/{id}/approve|unapprove`, `/payroll/pay`, `/kaspi/payouts`, `/kaspi/reconcile/{id}`. `SHOP_MANAGER`: `POST /orders`, `/orders/{id}/materials`. `BRIGADIER`: work entries. `VIEWER`: all reads. `DIRECTOR`: `/audit-log`. `ADMIN`: user management. | — | Least privilege holds; no finding. `GET /import-batches` requires ACCOUNTANT (conservative, safe direction). |
| CRED-1 | OK (verified) | Hardcoded credentials | grep over `app/` — no hardcoded passwords/tokens/keys (one hit is DSN masking in `app/core/config.py:190`); JWT secret from env (prior report) | — | None. |

## Escalation decisions (explicit)

- **SEC-002(b): P2 → P1.** The task's escalation rule is "real cross-IP access with reproducer" — met: a live reproducer proves an IP-B-restricted accountant can authorize (approve/pay) IP-A payroll money after a routine HR reassignment. Payroll approval is a financial authorization, not a read. The new `unapprove` endpoint (PAY-001) inherits the same exposure.
- **SEC-003: P2 → P1.** `del principal` is a complete absence of authorization on a company-wide financial-calendar write: any IP-restricted ACCOUNTANT can create periods affecting all IPs (and the overlap exclusion constraint lets one bad write block everyone's payroll). Option A (DIRECTOR/ADMIN-only) is implementable without owner; per-IP periods stay owner decision.
- **SEC-001 / SEC-002(a): stay P2.** Oracle only leaks ID existence; no data, action still blocked by 403. Fix together with SEC-002(b).

## Verified OK (no findings)

- `/accounts`, `/reports/account-balances`, `/reports/counterparty-balances`, `/pnl` — allow-list pattern correct at API and SQL layers.
- Transfer account↔IP binding enforced in service + both IPs checked in API.
- `uq_operations_import_fingerprint` per-IP; idempotency replay per-IP; race-safe savepoint.
- No raw-payload API exposure; Kaspi client stores response bodies only, secrets never logged.
- No hardcoded credentials; role matrix follows least privilege.

## Notes for implementers

- The SEC-002(b) fix (add `ip_id` to `payroll_summary` at calculate time) is a migration + backfill; existing summaries need `ip_id` derived from the employee's assignment at calculation — backfill from current assignment with a documented caveat, or leave historical rows NULL-tolerant with a strict check for new rows.
- Do not "fix" SEC-002(b) by blocking employee reassignment — reassignment is legitimate HR; the authorization must be temporal.
- The live reproducer script is at `/tmp/sec002b_repro.py` (ephemeral); the scenario should become a committed regression test when the fix lands.
