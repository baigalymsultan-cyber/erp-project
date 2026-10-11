"""
Alembic environment.

MASTER PROMPT references:
  §7  — Alembic is the migration tool;
  §10 — the connection URL comes from the environment, never from alembic.ini;
  §15 — migrations run with the session in UTC.

ADR-011: database/schema.sql is the source of truth for the physical schema.
Consequently `--autogenerate` is DISABLED for this project: comparing ORM
metadata against a schema that contains triggers, functions, EXCLUDE
constraints, partial indexes and views produces destructive diffs. Migrations
are written by hand; the models are then verified against the applied database
by tests/test_models_match_schema.py.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the application package importable when alembic is invoked from any cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_database_settings  # noqa: E402
from app.models import Base  # noqa: E402  (imports every model)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_database_settings()
config.set_main_option("sqlalchemy.url", settings.dsn())

#: Used only for reporting/inspection — autogenerate stays off (see docstring).
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=settings.dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=False,
        compare_server_default=False,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database in a single transaction."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = settings.dsn()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=settings.connect_args(),
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=False,
            compare_server_default=False,
            include_schemas=False,
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
