# Architecture Refactoring Phase — 2026-10-08

Branch: `arch-refactor-001` (from `fin-ops-0001@31a80f5`, tag `arch-freeze-2026-10-08`).
Report addressed: external architecture review, verdict CRITICAL (22 sections).

## R0 — done
- Tag `arch-freeze-2026-10-08`, branch `arch-refactor-001`.

## R1 — mechanical splits (zero behavior change) — done
- `app/api/__init__.py` (3162 lines, 72 routes) → `deps.py` + 12 APIRouter domain
  modules (auth, operations, transfers, orders, payroll, bank, kaspi,
  inventory, production, reports, reference, system). All 72 routes preserved.
- `app/services/__init__.py` (1347 lines) → payroll, orders, finance, kaspi,
  transfers, common (`_q2`). Re-export shim in `__init__.py`.
- `app/schemas/__init__.py` (462 lines) → common, finance, orders, kaspi,
  inventory, payroll, auth, bank. Re-export shim in `__init__.py`.
- `frontend/app.py` (1231 lines) → `app.py` (219, entrypoint), `theme.py`,
  `ui_kit.py`, `i18n/{ru,kk,en}.py`, `pages/{10 modules}.py`.
- **Repository layer deleted** (report §2, §18.8): `app/repositories/` was a
  196-line pass-through with 0 production usages. `TransferRepository.create`
  merely wrapped `add_transfer`. Removed + 1 obsolete test.
- **Privilege dedup** (§14): `app.core.security.ROLE_PRIVILEGE` is now the
  single source of truth; `frontend/auth_gate.py` imports it (no second table).
- **EN i18n** (§13): 164 keys translated; UI language selector now RU/KZ/EN.

## R2 — semantic fixes — done
- **§5 DoS**: bounded file upload (10 MiB, chunked read, HTTP 413),
  `MAX_STATEMENT_ROWS=100k` parser guard.
- **§6 strict decode**: `errors="replace"` removed; undecodable bytes reject
  the import (STOP over silent corruption).
- **§7 rate-limit**: `APP_ENV=production` requires `RATE_LIMIT_SHARED=1`
  (fail fast at startup, not silent misconfiguration).
- **§9 transactions**: policy documented in `app/api/deps.py`. 15 route-level
  `session.commit()` → `session.flush()` (atomic via `get_db`). Exceptions:
  bank_import ETL (own txns, idempotent), auth audit (independent session).
- **§11 logging**: `sanitize_text()` masks secrets in log messages + tracebacks
  (was: only `extra` fields masked).
- **§16 CI**: push trigger covers all branches (was master/main only).

## BLOCKED — no code changes (owner decisions required)
- **§4 reversal** (`reverse_operation`, status-flip POSTED→REVERSED): BLOCKED on
  Q1–Q7 (`docs/REVERSAL_DECISION_MEMO.md`). The report asks for a full rewrite
  to compensating operations; this needs owner answers on model A vs C,
  P&L semantics, partial reversal, etc. NOT touched.
- **§10 payroll accrual** (`operation_date=accrual_date=payment_date`): BLOCKED
  on TD-020. Semantics undefined; owner decision required. NOT touched.
- **KASPI_OQ_003** (payout/commission/settlement contracts): BLOCKED, external
  spec required. NOT touched.
- **REPORT_8**: BLOCKED, owner decision required. NOT touched.

## Verification
- `ruff check app etl frontend tests scripts`: clean on changed files
  (pre-existing test-file E501/I001 untouched).
- `mypy app`: 4 pre-existing errors (bank.py:330, transfers.py:38 ×3) —
  verified present at 31a80f5 via git stash; NOT introduced by refactor.
- `import app.api` (72 routes), `import app.services` (22 exports),
  `import app.schemas` (41 exports), `import frontend.app`: all OK.
- Full pytest: skipped (no PostgreSQL in this environment); CI will run it.
- Alembic: unchanged (no model/migration changes).

## Not done (out of scope for this pass)
- §8 "thin API": routes still contain business logic; full extraction to
  services is a larger semantic change — deferred.
- §17 CI gaps: coverage threshold, Docker build, SAST/DAST, SBOM — deferred.
- §12 frontend: further page decomposition possible — deferred.
