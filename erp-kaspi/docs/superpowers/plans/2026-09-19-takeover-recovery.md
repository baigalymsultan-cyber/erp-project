# Takeover Recovery and i18n Audit Implementation Plan

> **For agentic workers:** Execute task-by-task with TDD and verification-before-completion. Keep changes minimal and preserve TD-016/TD-015 semantics.

**Goal:** Re-establish a reproducible PostgreSQL-backed baseline, resolve verified recovery defects, and complete the repository-wide frontend i18n audit without weakening production safeguards.

**Architecture:** Keep the existing FastAPI/PostgreSQL/SQLAlchemy/Alembic/Streamlit architecture. First isolate infrastructure and fixture behavior, then audit the single `frontend.app.I18N` catalog through AST/static checks and runtime smoke tests. No new translation system and no production financial semantic changes.

**Tech Stack:** Python 3.12 runtime, FastAPI, PostgreSQL 15+, SQLAlchemy, Alembic, pytest, Ruff, mypy, Streamlit AppTest.

**Spec:** `file docs/REQUIREMENTS_MATRIX.md`, `file docs/TECHNICAL_DEBT.md`, `Master Prompt version 5.txt`.

## Global Constraints

- Do not alter closed TD-015 or TD-016 behavior without a reproduced defect.
- Do not add `ON DELETE CASCADE` without domain evidence.
- Never commit credentials or print secret values.
- Every bug fix needs a failing regression test before production code changes.
- A test run with teardown/session errors is not a passing run.

---

### Task 1: Verify recovery baseline

**Files:** No source changes.

- [ ] Record `git status`, HEAD, branch, recent commits, and current diff.

- [ ] Verify PostgreSQL availability, database connection, schema revision, and role presence without printing passwords.

- [ ] Run the targeted takeover tests and classify any teardown failure separately from test assertions.

### Task 2: Diagnose §94 cleanup

**Files:** `file conftest.py`, `file database/schema.sql`, relevant tests only if a defect is reproduced.

- [ ] Trace actual FK dependencies for `work_operation_types` and `work_entries` from PostgreSQL metadata and schema/migrations.

- [ ] Reproduce cleanup behavior with a committed child row if the existing fixture is unsafe.

- [ ] Add a minimal failing fixture-contract test before changing cleanup.

- [ ] Choose rollback, migrator purge, or ownership-aware deletion based on evidence; never use an unproven cascade.

- [ ] Run targeted cleanup tests and confirm no test data survives.

### Task 3: Complete i18n static audit

**Files:** `file frontend/app.py`, `file frontend/auth_gate.py`, `file frontend/admin_modules.py`, `file frontend/reports.py`, `file tests/test_i18n_catalog.py`.

- [ ] Use AST-based extraction for literal `t[...]`, `t.get(...)`, and equivalent catalog accesses.

- [ ] Compare used, RU, and KZ key sets; inspect duplicate literal keys by source order and Git history.

- [ ] Add failing regression tests for any missing/duplicate/placeholder defect.

- [ ] Apply the smallest catalog correction and verify runtime AppTest paths.

### Task 4: Verify BUG-001 and migration state

**Files:** Existing code/tests/docs only if evidence requires synchronization.

- [ ] Run the exact reset regression and inspect `clear_all()`/prune control flow.

- [ ] Run `alembic heads`, `current --check-heads`, and `check` against the test database.

- [ ] Compare models, migrations, and schema for the touched areas.

### Task 5: Regression and adversarial verification

**Files:** No additional source changes unless a reproduced issue remains.

- [ ] Run targeted, related, and full pytest suites.

- [ ] Repeat the full suite to check contamination/order dependence.

- [ ] Run Ruff, mypy, and migration checks.

- [ ] Review the final diff for secrets, generated artifacts, unrelated changes, and stale documentation.

- [ ] Update only evidence-backed documentation and commit only after all required checks are fresh.