#!/usr/bin/env bash
# =====================================================================
# STEP 1 helper — apply database/schema.sql and run the self-audit.
#
# The script is idempotent (§87): it can be run repeatedly.
# Use --recreate to drop and recreate the database from scratch.
#
# Usage:
#   scripts/apply_schema.sh                 # apply to $DB_NAME (default erp_dev)
#   scripts/apply_schema.sh --recreate      # drop + create + apply + audit
#   DB_NAME=erp_test scripts/apply_schema.sh --recreate
# =====================================================================
set -Eeuo pipefail

DB_NAME="${DB_NAME:-erp_dev}"
DB_USER="${DB_USER:-erp_app}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
DB_PASSWORD="${DB_PASSWORD:-devpass_local_only}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SCHEMA_FILE="$PROJECT_DIR/database/schema.sql"

if [[ ! -f "$SCHEMA_FILE" ]]; then
    echo "ERROR: schema file not found: $SCHEMA_FILE" >&2
    exit 1
fi

if [[ "${1:-}" == "--recreate" ]]; then
    echo ">>> Recreating database $DB_NAME"
    sudo -u postgres psql -q -c "DROP DATABASE IF EXISTS ${DB_NAME};"
    sudo -u postgres psql -q -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
    # CREATEDB: the restore drill (scripts/restore.sh, tests/test_backup_restore.py)
    # creates scratch databases as the app user. Schema DDL stays with erp_migrator.
    sudo -u postgres psql -q -c "CREATE ROLE ${DB_USER} LOGIN CREATEDB PASSWORD '${DB_PASSWORD}';"
    sudo -u postgres psql -q -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
fi

echo ">>> Applying schema to $DB_NAME"
PGPASSWORD="$DB_PASSWORD" psql -q -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
    -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$SCHEMA_FILE"

echo ">>> Verifying idempotency: applying the schema a second time (§87)"
PGPASSWORD="$DB_PASSWORD" psql -q -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
    -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$SCHEMA_FILE"

echo ">>> Running STEP 1 self-audit (§90)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="python3"
DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}" \
    "$PYTHON" "$PROJECT_DIR/scripts/audit_step1.py"
