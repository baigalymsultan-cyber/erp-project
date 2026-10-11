"""users.password_changed_at — MASTER PROMPT §17 password-change invalidation

Revision ID: 0002_users_password_changed_at
Revises: 0001_baseline_schema
Create Date: 2026-09-17

§17 (AUTHENTICATION) requires password-change invalidation: after a password
change (self-service or ADMIN reset) every JWT issued before that moment must
stop working. ``users.password_changed_at`` stores the boundary timestamp;
``app.core.security.current_principal`` compares the token ``iat`` claim
against it and returns 401 for older tokens.

ADR-011 keeps database/schema.sql as the source of truth: the column is added
to the canonical DDL as well, so a fresh ``apply_schema.sh`` database and an
``alembic upgrade head`` database are identical. This migration exists for
databases that were provisioned before the column was introduced.

The column is NULL by default — no existing user is force-logged-out on
deploy; invalidation starts from the first password change or reset.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0002_users_password_changed_at"
down_revision: str = "0001_baseline_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # §87 (re-runnable): 0001 applies database/schema.sql verbatim, and the
    # current schema.sql already declares the column — a database created by
    # 0001 alone therefore has it. Add it only when it is genuinely missing
    # (databases provisioned before the column was introduced).
    bind = op.get_bind()
    exists = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'users' "
            "AND column_name = 'password_changed_at'"
        )
    ).scalar()
    if exists is None:
        op.add_column(
            "users",
            sa.Column(
                "password_changed_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="MASTER PROMPT §17 — password-change invalidation: "
                "JWTs issued before this moment are rejected by "
                "current_principal(). NULL = never changed.",
            ),
            schema="public",
        )


def downgrade() -> None:
    op.drop_column("users", "password_changed_at", schema="public")
