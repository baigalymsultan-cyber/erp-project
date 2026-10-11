#!/usr/bin/env bash
# =====================================================================
# BACKUP — pg_dump of the ERP database (MASTER PROMPT §16, TD-004).
#
# The ledger is append-only (§16): backups are the recovery path for
# operator error and hardware failure. Never delete old backups here —
# retention is a separate decision (TD-004 plan).
#
# Usage:
#   DB_NAME=erp_dev scripts/backup.sh                    # custom-format dump
#   DB_NAME=erp_dev scripts/backup.sh --plain            # SQL text dump
#
# Output: backups/<db>_YYYYmmdd_HHMMSS.dump[.sql] + checksum file.
# Exit code 0 only if the dump file exists and is non-empty.
# =====================================================================
set -Eeuo pipefail

DB_NAME="${DB_NAME:-erp_dev}"
DB_USER="${DB_USER:-erp_app}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
export PGPASSWORD="${DB_PASSWORD:?DB_PASSWORD is required (§10 — no defaults for secrets)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
mkdir -p "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%d_%H%M%S)"
FORMAT_FLAG=()
EXT="dump"
if [[ "${1:-}" == "--plain" ]]; then
    FORMAT_FLAG=(-Fp)
    EXT="sql"
fi

OUT="$BACKUP_DIR/${DB_NAME}_${STAMP}.${EXT}"

pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -Fc "${FORMAT_FLAG[@]}" \
    -f "$OUT" "$DB_NAME"

if [[ ! -s "$OUT" ]]; then
    echo "ERROR: backup file is empty: $OUT" >&2
    exit 1
fi

sha256sum "$OUT" > "$OUT.sha256"

# Verify the archive is readable before declaring success (restore is the test).
if [[ -z "${FORMAT_FLAG[*]}" ]]; then
    pg_restore --list "$OUT" > /dev/null
fi

echo "BACKUP OK: $OUT"
echo "SHA256:    $OUT.sha256"
