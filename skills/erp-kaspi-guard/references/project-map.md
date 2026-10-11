# erp-kaspi-guard — references

## Key files

- Master Prompt: `~/workspace/user/files/Project_PromPt.txt` (governs)
- Baseline: `~/workspace/user/files/FINANCE-OPS-001.txt`
- Requirements: `~/workspace/erp-kaspi/docs/REQUIREMENTS_MATRIX.md`
- Debt/blockers: `~/workspace/erp-kaspi/docs/TECHNICAL_DEBT.md`
- Reversal memo: `~/workspace/erp-kaspi/docs/REVERSAL_DECISION_MEMO.md`
- TD-020: `~/workspace/erp-kaspi/docs/TD-020_PAYROLL_STATUS_DECISION.md`
- Kaspi: `~/workspace/erp-kaspi/docs/KASPI_OQ003_DISCOVERY_2026-10-03.md`

## Repo layout

- `app/api/__init__.py` — ~60 FastAPI endpoints
- `app/services/__init__.py` — financial mutations (add_transfer, add_operation,
  calculate_payroll, pay_payroll_summary, reconcile_payout_full, add_work_entry)
- `app/models/` — SQLAlchemy models (finance, hr, kaspi, orders, reference)
- `database/schema.sql` — source of truth (§7.3 audit triggers, §63 views)
- `alembic/versions/` — migrations (head: 0010 as of 2026-10-03)
- `frontend/` — Streamlit (app.py, reports.py — 7 reports, auth_gate.py)
- `etl/` — Kaspi Excel import

## Test environment

PostgreSQL 18 (ephemeral VM — rebuild if `pg_isready` fails):
- Binaries: `/home/pguser/pgdist/bin`, data: `/home/pguser/pgdata`
- DBs: `erp_test`, `erp_alembic_test`, `erp_dev`; roles: `erp_app`, `erp_migrator`
- Schema: `scripts/apply_schema.sh` (must print ALL CHECKS PASSED)
- Pytest env: `DATABASE_HOST=127.0.0.1 DATABASE_PORT=5432 DATABASE_NAME=erp_test
  DATABASE_USER=erp_app DATABASE_PASSWORD=test DB_MIGRATOR_PASSWORD=test
  TEST_DATABASE_MIGRATOR_PASSWORD=test`, plus `LD_LIBRARY_PATH=/home/pguser/pgdist/lib`

## Standing decisions (2026-10-03)

- C-1/C-2 idempotency: `Idempotency-Key` header → `import_fingerprint`
  (unique partial index), replay returns `IDEMPOTENT_REPLAY`
- 29 tables have audit triggers (`trg_*_audit` → `fn_audit()`)
- i18n: RU/KK symmetric (~150 keys); EN catalog is a known gap
- Excel export: raw numbers, never formatted strings (§62/DEF-006, ADR-013: no PDF)
