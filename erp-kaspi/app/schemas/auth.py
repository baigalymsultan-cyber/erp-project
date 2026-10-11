"""Auth schemas — §52/§55: users, login, tokens."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# --------------------------------------------------------------------------- #
# Auth — §52/§55: users, login, tokens
# --------------------------------------------------------------------------- #


class UserCreate(BaseModel):
    """Payload for POST /auth/users (ADMIN only, §53)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=10, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = None
    roles: list[str] = Field(default_factory=list, max_length=7)


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    """Self-service password change (MASTER PROMPT §17).

    The current password must be re-presented; on success every JWT issued
    before ``now`` is revoked via ``users.password_changed_at``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class PasswordResetRequest(BaseModel):
    """ADMIN-mediated password reset (MASTER PROMPT §17 reset flow).

    No e-mail infrastructure exists in the deployment, so reset is
    ADMIN-set: the admin assigns a temporary password, the user changes it
    via /auth/password-change. The reset revokes all of the target's
    previously issued tokens (password_changed_at).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    new_password: str = Field(min_length=10, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    roles: list[str]
