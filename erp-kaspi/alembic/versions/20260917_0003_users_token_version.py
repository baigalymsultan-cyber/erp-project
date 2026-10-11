"""users.token_version — MASTER PROMPT §17 server-side token revocation

Revision ID: 0003_users_token_version
Revises: 0002_users_password_changed_at
Create Date: 2026-09-17

§17 requires token revocation and session invalidation. A JWT is stateless,
so revocation needs server-side state: the user carries an integer
token_version; every issued JWT embeds the version at issue time, and
current_principal() rejects tokens whose version differs from the stored
one. /auth/logout (self) and POST /auth/users/{id}/revoke-sessions (ADMIN)
bump the version, instantly invalidating all of the user's sessions.

Per-token (individual) revocation is deliberately not implemented: for the
single-process internal deployment the per-user version covers every
documented scenario (logout, emergency revocation, credential compromise)
without a denylist table and a write per request — see docs/ADR.md.

ADR-011 keeps database/schema.sql as the source of truth; the column is
added there as well. Like 0002, the add is conditional (§87 re-runnable).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0003_users_token_version"
down_revision: str = "0002_users_password_changed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'users' "
            "AND column_name = 'token_version'"
        )
    ).scalar()
    if exists is None:
        op.add_column(
            "users",
            sa.Column(
                "token_version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
                comment="MASTER PROMPT §17 — server-side token revocation: JWTs "
                "carry the version at issue; current_principal() rejects tokens "
                "whose version differs from the stored one. /auth/logout and the "
                "ADMIN revoke-sessions endpoint bump it, instantly killing every "
                "session.",
            ),
            schema="public",
        )

    # audit_log action vocabulary gains REVOKE_SESSIONS (same §17 change).
    op.drop_constraint("chk_audit_log_action", "audit_log", schema="public")
    op.create_check_constraint(
        "chk_audit_log_action",
        "audit_log",
        "action IN ("
        "'INSERT', 'UPDATE', 'DELETE', "
        "'CREATE', 'CORRECTION', 'PAYMENT', 'REFUND', "
        "'STATUS_CHANGE', 'RATE_CHANGE', 'PERMISSION_CHANGE', 'FINANCIAL_CHANGE', "
        "'LOGIN', 'LOGIN_FAILED', 'LOGOUT', 'REVOKE_SESSIONS'"
        ")",
        schema="public",
    )
    op.drop_constraint("chk_audit_log_payload", "audit_log", schema="public")
    op.create_check_constraint(
        "chk_audit_log_payload",
        "audit_log",
        "old_data IS NOT NULL OR new_data IS NOT NULL OR action IN "
        "('LOGIN', 'LOGIN_FAILED', 'LOGOUT', 'REVOKE_SESSIONS')",
        schema="public",
    )


def downgrade() -> None:
    op.drop_constraint("chk_audit_log_action", "audit_log", schema="public")
    op.create_check_constraint(
        "chk_audit_log_action",
        "audit_log",
        "action IN ("
        "'INSERT', 'UPDATE', 'DELETE', "
        "'CREATE', 'CORRECTION', 'PAYMENT', 'REFUND', "
        "'STATUS_CHANGE', 'RATE_CHANGE', 'PERMISSION_CHANGE', 'FINANCIAL_CHANGE', "
        "'LOGIN', 'LOGIN_FAILED', 'LOGOUT'"
        ")",
        schema="public",
    )
    op.drop_constraint("chk_audit_log_payload", "audit_log", schema="public")
    op.create_check_constraint(
        "chk_audit_log_payload",
        "audit_log",
        "old_data IS NOT NULL OR new_data IS NOT NULL OR action IN "
        "('LOGIN', 'LOGIN_FAILED', 'LOGOUT')",
        schema="public",
    )
    op.drop_column("users", "token_version", schema="public")
