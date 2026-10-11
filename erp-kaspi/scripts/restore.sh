#!/usr/bin/env bash
# =====================================================================
# RESTORE — pg_restore of a backup produced by scripts/backup.sh.
#
# A backup that has never been restored is a hope, not a plan (§16):
# this script is exercised in CI and before every release.
#
# Usage:
#   scripts/restore.sh backups/erp_dev_20260903_150000.dump DB_NAME=erp_restored
#
# The target database must NOT be the live one (the script refuses to
# restore over erp_dev/erp_prod unless FORCE=1).
# =====================================================================
set -Eeuo pipefail

DUMP_FILE="${1:?usage: scripts/restore.sh <dump-file> [target-db]}"
TARGET_DB="${TARGET_DB:-erp_restored}"
DB_USER="${DB_USER:-erp_app}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
export PGPASSWORD="${DB_PASSWORD:?DB_PASSWORD is required (§10)}"

case "$TARGET_DB" in
    erp_dev|erp_prod)
        if [[ "${FORCE:-0}" != "1" ]]; then
            echo "REFUSING: restoring over $TARGET_DB is destructive." >&2
            echo "Set TARGET_DB to a scratch database, or FORCE=1 to override." >&2
            exit 2
        fi
        ;;
esac

[[ -f "$DUMP_FILE" ]] || { echo "ERROR: dump not found: $DUMP_FILE" >&2; exit 1; }

# Verify checksum when the sidecar file exists (§16 — integrity first).
if [[ -f "$DUMP_FILE.sha256" ]]; then
    sha256sum -c "$DUMP_FILE.sha256" --status \
        || { echo "ERROR: checksum mismatch: $DUMP_FILE" >&2; exit 1; }
fi

if ! psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '$TARGET_DB'" | grep -q 1; then
    createdb -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -T template0 "$TARGET_DB"
fi

pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
    --no-owner --no-privileges --exit-on-error -d "$TARGET_DB" "$DUMP_FILE"

TABLES="$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$TARGET_DB" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
echo "RESTORE OK: $TARGET_DB ($TABLES objects in public)"
