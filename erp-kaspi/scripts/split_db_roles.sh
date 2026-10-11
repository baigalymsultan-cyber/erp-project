#!/usr/bin/env bash
# =====================================================================
# SPLIT DB ROLES — TD-008 (MASTER PROMPT §15 / §55 audit item 10).
#
# Least privilege: the application role must hold DML only; schema
# ownership and DDL belong to a dedicated migrator role.
#
#   erp_migrator  — owns the schema, tables, sequences, views, functions;
#                   the only role allowed to run migrations (alembic)
#                   and apply database/schema.sql.
#   erp_app       — USAGE + SELECT/INSERT/UPDATE/DELETE on tables,
#                   USAGE/SELECT on sequences, EXECUTE on functions.
#                   CREATE on the schema is revoked (§15).
#
# The script is idempotent (§87): ownership is only transferred when it
# differs, grants are re-asserted, default privileges are re-published
# so future migrations keep the app role working.
#
# Usage:
#   DB_NAME=erp_test scripts/split_db_roles.sh
#   DB_NAME=erp_dev DB_APP_USER=erp_app DB_MIGRATOR_PASSWORD=... \
#     scripts/split_db_roles.sh
#
# Admin access: by default local peer auth `sudo -u postgres psql`
# (sandbox / local dev). On CI (postgres runs as a TCP service with
# password auth) set DB_ADMIN_HOST, e.g.:
#   DB_NAME=erp_test DB_ADMIN_HOST=127.0.0.1 DB_ADMIN_PASSWORD=postgres \
#     scripts/split_db_roles.sh
# =====================================================================
set -Eeuo pipefail

DB_NAME="${DB_NAME:-erp_dev}"
DB_APP_USER="${DB_APP_USER:-erp_app}"
DB_MIGRATOR_USER="${DB_MIGRATOR_USER:-erp_migrator}"
DB_MIGRATOR_PASSWORD="${DB_MIGRATOR_PASSWORD:?DB_MIGRATOR_PASSWORD is required (§10 — no defaults for secrets)}"

if [[ -n "${DB_ADMIN_HOST:-}" ]]; then
    psql_admin() {
        PGPASSWORD="${DB_ADMIN_PASSWORD:?DB_ADMIN_PASSWORD is required with DB_ADMIN_HOST}" \
            psql -h "$DB_ADMIN_HOST" -p "${DB_ADMIN_PORT:-5432}" \
            -U "${DB_ADMIN_USER:-postgres}" -v ON_ERROR_STOP=1 -d "$DB_NAME" -qtA "$@"
    }
    psql_admin_any() {
        PGPASSWORD="${DB_ADMIN_PASSWORD:?DB_ADMIN_PASSWORD is required with DB_ADMIN_HOST}" \
            psql -h "$DB_ADMIN_HOST" -p "${DB_ADMIN_PORT:-5432}" \
            -U "${DB_ADMIN_USER:-postgres}" -v ON_ERROR_STOP=1 -qtA "$@"
    }
else
    psql_admin() {
        sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -qtA "$@"
    }
    psql_admin_any() {
        sudo -u postgres psql -v ON_ERROR_STOP=1 -qtA "$@"
    }
fi

echo ">>> TD-008: splitting roles on $DB_NAME (app=$DB_APP_USER, migrator=$DB_MIGRATOR_USER)"

# 1. The migrator role must exist (password only set on create; a re-run
#    must not clobber a rotated secret).
psql_admin_any -c "SELECT 1 FROM pg_roles WHERE rolname='${DB_MIGRATOR_USER}'" | grep -q 1 \
    || psql_admin_any -c \
        "CREATE ROLE ${DB_MIGRATOR_USER} LOGIN PASSWORD '${DB_MIGRATOR_PASSWORD}';"

# 2. Ownership: schema, tables, sequences, views and functions move to the
#    migrator. The ledger tables stay byte-identical — only ownership and
#    privileges change (no data, no structure).
psql_admin <<SQL
DO \$\$
DECLARE r record;
BEGIN
    -- PG15: the default schema is owned by the pseudo-role pg_database_owner
    -- (i.e. by whoever owns the database — the app role in CI/dev), so the
    -- transfer must not key on the owner name. ALTER ... OWNER is idempotent.
    ALTER SCHEMA public OWNER TO ${DB_MIGRATOR_USER};
    FOR r IN SELECT tablename FROM pg_tables
             WHERE schemaname = 'public' AND tableowner = '${DB_APP_USER}'
    LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO ${DB_MIGRATOR_USER}', r.tablename);
    END LOOP;
    FOR r IN SELECT sequencename FROM pg_sequences
             WHERE schemaname = 'public' AND sequenceowner = '${DB_APP_USER}'
    LOOP
        EXECUTE format('ALTER SEQUENCE public.%I OWNER TO ${DB_MIGRATOR_USER}', r.sequencename);
    END LOOP;
    FOR r IN SELECT viewname FROM pg_views
             WHERE schemaname = 'public' AND viewowner = '${DB_APP_USER}'
    LOOP
        EXECUTE format('ALTER VIEW public.%I OWNER TO ${DB_MIGRATOR_USER}', r.viewname);
    END LOOP;
    -- regprocedure renders the full signature (name + argument types),
    -- which ALTER FUNCTION requires for overloads (unlike %I on a name).
    FOR r IN SELECT p.oid::regprocedure::text AS signature
             FROM pg_proc p
             JOIN pg_namespace n ON n.oid = p.pronamespace
             JOIN pg_roles ro ON ro.oid = p.proowner
             WHERE n.nspname = 'public' AND ro.rolname = '${DB_APP_USER}'
    LOOP
        EXECUTE format('ALTER FUNCTION public.%s OWNER TO ${DB_MIGRATOR_USER}',
                       r.signature);
    END LOOP;
END
\$\$;
SQL

# 3. Least privilege for the app role (§15): DML everywhere, no DDL.
psql_admin <<SQL
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM ${DB_APP_USER};
GRANT USAGE ON SCHEMA public TO ${DB_APP_USER};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ${DB_APP_USER};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ${DB_APP_USER};
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO ${DB_APP_USER};
ALTER DEFAULT PRIVILEGES FOR ROLE ${DB_MIGRATOR_USER} IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ${DB_APP_USER};
ALTER DEFAULT PRIVILEGES FOR ROLE ${DB_MIGRATOR_USER} IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO ${DB_APP_USER};
ALTER DEFAULT PRIVILEGES FOR ROLE ${DB_MIGRATOR_USER} IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO ${DB_APP_USER};
SQL

# 4. Evidence: fail loudly if the app role still holds any ownership.
LEFTOVER="$(psql_admin -c "
    SELECT (SELECT count(*) FROM pg_tables
              WHERE schemaname='public' AND tableowner='${DB_APP_USER}')
         + (SELECT count(*) FROM pg_sequences
              WHERE schemaname='public' AND sequenceowner='${DB_APP_USER}')
         + (SELECT count(*) FROM pg_views
              WHERE schemaname='public' AND viewowner='${DB_APP_USER}')
         + (SELECT count(*) FROM information_schema.schemata
              WHERE schema_name='public' AND schema_owner='${DB_APP_USER}');
")"
if [[ "$LEFTOVER" != "0" ]]; then
    echo "ERROR: ${DB_APP_USER} still owns $LEFTOVER object(s) in $DB_NAME" >&2
    exit 1
fi

echo ">>> OK: schema owned by ${DB_MIGRATOR_USER}; ${DB_APP_USER} is DML-only (evidence: 0 owned objects)"
