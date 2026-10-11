#!/usr/bin/env bash
# =====================================================================
# Verification gate — runs INSIDE docker/gate (see scripts/run_gate.sh).
# Mirrors .github/workflows/ci.yml step by step, then adds the
# FIN-OPS-0001 checks: migration round-trip and a DB-level mutation test.
# Throwaway CI credentials only; the database is an empty tmpfs PG 15.
# =====================================================================
set -Eeuo pipefail

export DATABASE_HOST=pg DATABASE_PORT=5432
export DATABASE_USER=erp_app DATABASE_PASSWORD=erp_ci_pass
export DB_MIGRATOR_PASSWORD=erp_ci_migrator_pass
export DATABASE_NAME=erp_test TEST_DATABASE_NAME=erp_test
export PYTHONPATH=/repo PGPASSWORD=postgres

LOG_DIR=/repo/gate_logs
mkdir -p "$LOG_DIR"
SUMMARY="$LOG_DIR/summary.txt"
: > "$SUMMARY"
step() { echo; echo "================ $* ================"; }
ok()   { echo "PASS  $*" | tee -a "$SUMMARY"; }

step "0. Environment"
psql -h pg -U postgres -tAc "SELECT version()" | tee -a "$SUMMARY"
python --version | tee -a "$SUMMARY"
git config --global --add safe.directory /repo
git -C /repo rev-parse --abbrev-ref HEAD | sed 's/^/branch: /' | tee -a "$SUMMARY"
git -C /repo rev-parse HEAD | sed 's/^/HEAD:   /' | tee -a "$SUMMARY"

step "1. Provision roles and databases (as CI)"
psql -h pg -U postgres -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE ROLE erp_app LOGIN PASSWORD 'erp_ci_pass';
CREATE ROLE erp_migrator LOGIN PASSWORD 'erp_ci_migrator_pass';
CREATE DATABASE erp_test OWNER erp_migrator;
CREATE DATABASE erp_alembic_test OWNER erp_migrator;
SQL
test "$(psql -h pg -U postgres -d erp_alembic_test -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public'")" = "0"
ok "fresh erp_alembic_test has 0 public tables"

step "2. Apply schema twice (idempotency, §87) + self-audit"
DB_NAME=erp_test DB_HOST=pg DB_USER=erp_migrator DB_PASSWORD=erp_ci_migrator_pass \
  bash scripts/apply_schema.sh
DB_NAME=erp_test DB_HOST=pg DB_USER=erp_migrator DB_PASSWORD=erp_ci_migrator_pass \
  bash scripts/apply_schema.sh
ok "schema.sql applied twice"

step "3. TD-008 role split"
DB_NAME=erp_test DB_ADMIN_HOST=pg DB_ADMIN_PASSWORD=postgres \
  DB_APP_USER=erp_app DB_MIGRATOR_PASSWORD=erp_ci_migrator_pass \
  bash scripts/split_db_roles.sh
ok "role split"

step "4. STEP 1 self-audit as erp_app"
DATABASE_URL="postgresql://erp_app:erp_ci_pass@pg:5432/erp_test" \
  python scripts/audit_step1.py | tee "$LOG_DIR/audit.txt"
{ grep -Eo '[0-9]+/[0-9]+' "$LOG_DIR/audit.txt" || echo "see gate_logs/audit.txt"; } \
  | tail -1 | sed 's/^/self-audit: /' | tee -a "$SUMMARY"

step "5. Alembic on a clean database: upgrade, round-trip, check"
mig() { DATABASE_NAME=erp_alembic_test DATABASE_USER=erp_migrator \
        DATABASE_PASSWORD=erp_ci_migrator_pass alembic "$@"; }
mig upgrade head
mig heads | tee -a "$SUMMARY"
mig current | tee -a "$SUMMARY"
mig downgrade -1
mig upgrade head
ok "0007 downgrade/upgrade round-trip"
DATABASE_NAME=erp_alembic_test alembic check
ok "alembic check (no drift)"

step "6. Full pytest (warnings are errors)"
python -m pytest -W error::DeprecationWarning 2>&1 | tee "$LOG_DIR/pytest.txt"
tail -1 "$LOG_DIR/pytest.txt" | sed 's/^/pytest: /' | tee -a "$SUMMARY"
if grep -Eq '[0-9]+ skipped' "$LOG_DIR/pytest.txt"; then
  echo "WARN  some tests were SKIPPED — read gate_logs/pytest.txt" | tee -a "$SUMMARY"
fi

step "7. Lint / types / whitespace"
python -m ruff check . && ok "ruff check ."
python -m mypy app && ok "mypy app"
git -C /repo diff --check && ok "git diff --check"

step "8. FIN-OPS-0001 mutation test (DB function only; source files untouched)"
python - <<'PY'
import re, subprocess, sys
from pathlib import Path
body = re.search(
    r"CREATE OR REPLACE FUNCTION fn_protect_posted_ledger_operation\(\).*?\n\$\$;\n",
    Path("database/schema.sql").read_text(encoding="utf-8"), re.S).group(0)
mutant = body.replace("NEW.amount IS DISTINCT FROM OLD.amount OR", "", 1)
assert mutant != body, "mutation anchor not found"
def apply(sql):
    subprocess.run(["psql", "-h", "pg", "-U", "erp_migrator", "-d", "erp_test",
                    "-v", "ON_ERROR_STOP=1", "-q"], input=sql, text=True, check=True,
                   env={"PGPASSWORD": "erp_ci_migrator_pass", "PATH": "/usr/bin:/bin"})
apply(mutant)
try:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q",
                        "tests/test_ledger_immutability.py", "-k", "amount"])
finally:
    apply(body)  # always restore the real guard
if r.returncode == 0:
    sys.exit("MUTANT SURVIVED: removing the amount check did not fail any test")
print("mutant killed: amount tests failed as expected; guard restored")
PY
python -m pytest -q tests/test_ledger_immutability.py
ok "mutation killed + guard restored + targeted tests green"

step "SUMMARY"
cat "$SUMMARY"
echo "Full logs: gate_logs/ (pytest.txt, audit.txt, summary.txt)"
