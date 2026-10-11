"""
Authentication, RBAC and audit models.

1:1 mapping of database/schema.sql:
  users       [ARCHITECTURAL EXTENSION, §52, §55]
  roles       [ARCHITECTURAL EXTENSION, §52, §53]
  user_roles  [ARCHITECTURAL EXTENSION, §52]
  audit_log   [ARCHITECTURAL EXTENSION, §54]
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import (
    TIMESTAMPTZ,
    Base,
    bool_column,
    created_at_column,
    pk_column,
    updated_at_column,
)

#: Actions accepted by audit_log.action (§54).
AUDIT_ACTIONS: tuple[str, ...] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE",
    "CORRECTION",
    "PAYMENT",
    "REFUND",
    "STATUS_CHANGE",
    "RATE_CHANGE",
    "PERMISSION_CHANGE",
    "FINANCIAL_CHANGE",
    "LOGIN",
    "LOGIN_FAILED",
    "LOGOUT",
    "REVOKE_SESSIONS",
)

#: The seven mandatory RBAC roles of §52.
SYSTEM_ROLE_CODES: tuple[str, ...] = (
    "ADMIN",
    "DIRECTOR",
    "ACCOUNTANT",
    "HR",
    "SHOP_MANAGER",
    "BRIGADIER",
    "VIEWER",
)


class User(Base):
    """ARCHITECTURAL EXTENSION — application user (§52, §55)."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("username ~ '^[A-Za-z0-9._-]{3,100}$'", name="chk_users_username"),
        CheckConstraint(
            r"email IS NULL OR email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$'",
            name="chk_users_email",
        ),
        # §55: passwords are stored only as a secure hash, never in plaintext.
        CheckConstraint(
            r"password_hash ~ '^\$(2[aby]|argon2(id|i|d)|scrypt)\$'",
            name="chk_users_password_hash",
        ),
        CheckConstraint("failed_logins >= 0", name="chk_users_failed_logins"),
        Index("idx_users_employee", "employee_id"),
        {"comment": "ARCHITECTURAL EXTENSION — application users (MASTER PROMPT §52, §55)."},
    )

    id: Mapped[int] = pk_column()
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Secure password hash only (bcrypt/argon2/scrypt). "
        "Plaintext is rejected by CHECK (§55).",
    )
    is_active: Mapped[bool] = bool_column(default=True)
    employee_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    failed_logins: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    locked_until: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    #: MASTER PROMPT §17 — password-change invalidation: tokens issued before
    #: this instant are rejected by current_principal(). NULL = never changed.
    password_changed_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMPTZ, nullable=True
    )
    #: MASTER PROMPT §17 — server-side token revocation anchor for JWTs.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    role_links: Mapped[list[UserRole]] = relationship(
        back_populates="user",
        foreign_keys="UserRole.user_id",
        cascade="all, delete-orphan",
    )
    roles: Mapped[list[Role]] = relationship(
        secondary="user_roles",
        primaryjoin="User.id == UserRole.user_id",
        secondaryjoin="Role.id == UserRole.role_id",
        viewonly=True,
    )


class Role(Base):
    """ARCHITECTURAL EXTENSION — RBAC role (§52, §53)."""

    __tablename__ = "roles"
    __table_args__ = (
        # Minimum length is 2: the mandatory role 'HR' (§52) is two characters.
        CheckConstraint("code ~ '^[A-Z][A-Z_]{1,49}$'", name="chk_roles_code"),
        {
            "comment": "ARCHITECTURAL EXTENSION — RBAC roles: ADMIN, DIRECTOR, ACCOUNTANT, "
            "HR, SHOP_MANAGER, BRIGADIER, VIEWER (MASTER PROMPT §52, §53)."
        },
    )

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = bool_column(default=False)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()


class UserRole(Base):
    """ARCHITECTURAL EXTENSION — user ↔ role assignment (§52)."""

    __tablename__ = "user_roles"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "role_id", name="pk_user_roles"),
        Index("idx_user_roles_role", "role_id"),
        {"comment": "ARCHITECTURAL EXTENSION — user ↔ role assignment (MASTER PROMPT §52)."},
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("roles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    granted_at: Mapped[datetime.datetime] = created_at_column()
    granted_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    user: Mapped[User] = relationship(back_populates="role_links", foreign_keys=[user_id])
    role: Mapped[Role] = relationship(foreign_keys=[role_id])
    granter: Mapped[User | None] = relationship(foreign_keys=[granted_by], viewonly=True)


class AuditLog(Base):
    """
    ARCHITECTURAL EXTENSION — append-only audit trail (§54).

    Rows are written by the database trigger fn_audit(); secrets
    (password_hash, encrypted_token, token, secret) are replaced with
    '***MASKED***' before being stored.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            "action IN ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'CORRECTION', 'PAYMENT', "
            "'REFUND', 'STATUS_CHANGE', 'RATE_CHANGE', 'PERMISSION_CHANGE', "
            "'FINANCIAL_CHANGE', 'LOGIN', 'LOGIN_FAILED', 'LOGOUT', "
            "'REVOKE_SESSIONS')",
            name="chk_audit_log_action",
        ),
        CheckConstraint(
            "old_data IS NOT NULL OR new_data IS NOT NULL "
            "OR action IN ('LOGIN', 'LOGIN_FAILED', 'LOGOUT', 'REVOKE_SESSIONS')",
            name="chk_audit_log_payload",
        ),
        Index("idx_audit_log_entity", "entity", "entity_id", "created_at"),
        Index("idx_audit_log_user", "user_id", "created_at"),
        Index("idx_audit_log_created", "created_at"),
        {
            "comment": "ARCHITECTURAL EXTENSION — append-only audit trail "
            "(MASTER PROMPT §54). Secrets are masked by fn_audit()."
        },
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="NULL when the change was made by a migration, ETL job or DBA — never faked.",
    )
    entity: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    old_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = created_at_column()

    user: Mapped[User | None] = relationship()


class AuthRateEvent(Base):
    """
    TD-015 / ADR-014 — shared sliding-window store for the §55 auth rate
    limiter. One row per rate-limited attempt; the limiter COUNTs rows in
    (now - window, now] for the bucket key and prunes rows beyond the
    retention horizon. No FK to users on purpose: unknown usernames and
    pre-auth IP floods must also be limited (credential stuffing).
    """

    __tablename__ = "auth_rate_events"
    __table_args__ = (
        Index(
            "idx_auth_rate_events_bucket_time",
            "bucket_key",
            "occurred_at",
        ),
        {
            "comment": (
                "TD-015/ADR-014 — shared auth rate-limit events (§55); "
                "sliding window over bucket_key; prune rows beyond the "
                "retention horizon."
            )
        },
    )

    id: Mapped[int] = pk_column()
    bucket_key: Mapped[str] = mapped_column(String(256), nullable=False)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


__all__ = [
    "AUDIT_ACTIONS",
    "SYSTEM_ROLE_CODES",
    "AuditLog",
    "AuthRateEvent",
    "Role",
    "User",
    "UserRole",
]
