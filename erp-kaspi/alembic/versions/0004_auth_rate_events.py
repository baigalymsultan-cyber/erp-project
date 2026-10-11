"""TD-015 — distributed auth rate limiting (shared backend).

Revision ID: 0004_auth_rate_events
Revises: 0003_users_token_version
Create Date: 2026-09-18

The in-process rate limiter (app/core/security.py, §55) keeps its sliding
window in one process's memory: with multiple uvicorn workers or replicas
an attacker multiplies the effective attempt budget by the worker count.
ADR-014: shared state lives in the existing PostgreSQL database (no new
Redis dependency) in the auth_rate_events table. The login path already
requires a DB round-trip, so fail-closed on DB unavailability is
consistent with the rest of auth.

Design notes:
- No FK on user: the limiter must also rate-limit unknown usernames
  (credential stuffing against non-existent accounts) and pre-auth IP
  floods; a per-user FK would couple the table to users and break the
  unknown-username case.
- occurred_at is TIMESTAMPTZ NOT NULL; a BRIN-ish btree index supports
  the sliding-window COUNT and opportunistic pruning.
- No unique constraint: many events per bucket per window is the point.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_auth_rate_events"
down_revision = "0003_users_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'auth_rate_events'"
        )
    ).scalar()
    if exists is not None:
        return
    op.create_table(
        "auth_rate_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "bucket_key",
            sa.String(length=256),
            nullable=False,
            comment="limiter dimension, e.g. 'login:ip:username' (§55)",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="public",
    )
    op.create_index(
        "idx_auth_rate_events_bucket_time",
        "auth_rate_events",
        ["bucket_key", "occurred_at"],
        schema="public",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_auth_rate_events_bucket_time",
        table_name="auth_rate_events",
        schema="public",
    )
    op.drop_table("auth_rate_events", schema="public")
