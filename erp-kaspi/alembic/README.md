# Alembic migrations (canonical)

`alembic.ini` at the repository root points here (`script_location = alembic`).
This directory contains ONLY what the migration tool needs:

- `env.py` — Alembic environment; the connection URL comes from
  `app.core.config.DatabaseSettings` (environment variables, §10);
- `script.py.mako` — revision template;
- `versions/` — migration revisions (baseline: `20260828_0000_0001_baseline_schema.py`).

ADR-011: `database/schema.sql` is the source of truth for the physical schema;
`--autogenerate` is disabled (see `env.py`). Model ↔ migration drift is checked
by `alembic check` in CI and by `tests/test_alembic_baseline.py`.

Hygiene (§42, Phase 3 2026-09-15): this directory previously contained a full
stale copy of an old working tree (~55 MB: duplicate `app/`, `docs/`, `tests/`,
zip archives, `Skills/`, `Articles/`, old backups and CI). It was removed;
migration files themselves are unchanged (baseline verified byte-identical
before removal).
