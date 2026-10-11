#!/bin/bash
# =====================================================================
# TD-008 — container bootstrap: create the DML-only application role.
#
# The official postgres image creates exactly one superuser (POSTGRES_USER
# = erp_migrator in docker-compose.yml): the role that owns the schema and
# runs migrations. The API process must never connect with that role, so
# this script (docker-entrypoint runs *.sh with shell expansion, after the
# alphabetically earlier 01-schema.sql) creates erp_app with the same
# least-privilege grant set as scripts/split_db_roles.sh (§15).
# =====================================================================
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE erp_app LOGIN PASSWORD '${DB_APP_PASSWORD:?set DB_APP_PASSWORD (§10)}';
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    REVOKE CREATE ON SCHEMA public FROM erp_app;
    GRANT USAGE ON SCHEMA public TO erp_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO erp_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO erp_app;
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO erp_app;
    ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO erp_app;
    ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO erp_app;
    ALTER DEFAULT PRIVILEGES FOR ROLE erp_migrator IN SCHEMA public
        GRANT EXECUTE ON FUNCTIONS TO erp_app;
EOSQL
