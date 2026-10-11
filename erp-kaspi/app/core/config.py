"""
Database connection settings.

MASTER PROMPT references:
  §10 — secrets are read from the environment, never hard-coded, never
        committed to the repository;
  §11 — .env.example documents every required variable;
  §15 — the session works in UTC; Asia/Almaty is a presentation concern.

Only the settings needed by the database / migration layer are defined here.
The full application settings object (JWT, Kaspi, logging, …) is created in
DAY 3 of PLAN v1.0 together with app/core/crypto.py and app/core/logging.py;
adding those fields now would produce configuration with no consumer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote

#: Default values used only for a local sandbox database. They are NOT
#: production credentials and must be overridden by the environment (§10).
_DEFAULTS: dict[str, str] = {
    "DATABASE_HOST": "127.0.0.1",
    "DATABASE_PORT": "5432",
    "DATABASE_NAME": "erp_dev",
    "DATABASE_USER": "erp_app",
    "DATABASE_PASSWORD": "",
}


class ConfigurationError(RuntimeError):
    """Raised when the environment does not contain a usable configuration."""


class Secret:
    """
    A string that does not reveal itself when rendered (§10).

    TD-011: a plain `str` field on a dataclass leaks through the generated
    __repr__, and from there into tracebacks, `logging.exception`, error
    trackers and pytest output. Wrapping the value means the leak has to be
    written deliberately as `.reveal()` — greppable in review — instead of
    happening by default.

    Covered: repr(), str(), f-strings, %-formatting, format(), logging of the
    containing object, and equality against the raw string (which would
    otherwise let a test confirm a secret by guessing it).

    Deliberately NOT a dataclass: dataclasses.asdict() recurses into nested
    dataclass fields and would rebuild the secret as {"_value": "..."},
    defeating the whole wrapper. That leak was found by
    tests/test_core_database.py::test_settings_dict_dump_does_not_leak.

    __slots__ without __dict__ also keeps the value out of vars()/__dict__
    dumps used by naive serialisers.
    """

    __slots__ = ("_value",)

    _value: str

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise ConfigurationError(f"Secret expects a str, got {type(value).__name__}")
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: object) -> None:
        """Immutable: a secret must not be replaced after configuration."""
        raise AttributeError("Secret is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Secret is immutable")

    def __reduce__(self) -> tuple[type[Secret], tuple[str]]:
        """
        Support copy / deepcopy / pickle without weakening immutability.

        copy.deepcopy() (used internally by dataclasses.asdict) reconstructs an
        object by writing attributes directly, which __setattr__ forbids.
        Rebuilding through the constructor keeps both behaviours intact. Note
        that pickling a Secret still serialises the value — pickled config must
        be treated as a secret-bearing artefact.
        """
        return (type(self), (self._value,))

    def reveal(self) -> str:
        """Return the raw secret. The only sanctioned way to read it."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        """Length without content — enough to distinguish set from unset."""
        return len(self._value)

    def __repr__(self) -> str:
        return "Secret(***)" if self._value else "Secret(empty)"

    def __str__(self) -> str:
        return "***" if self._value else ""

    def __format__(self, format_spec: str) -> str:
        # Without this, f"{secret:>10}" would fall back to object.__format__
        # and could expose the value on some Python versions.
        return format(str(self), format_spec)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """PostgreSQL connection settings assembled from the environment (§10)."""

    host: str
    port: int
    name: str
    user: str
    #: Wrapped so that repr()/str()/logging cannot expose it (TD-011).
    password: Secret
    #: Statement timeout in milliseconds applied to every connection.
    statement_timeout_ms: int = 30_000
    #: Application name reported to PostgreSQL (visible in pg_stat_activity).
    application_name: str = "erp-backend"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> DatabaseSettings:
        """Build settings from environment variables (§10, §11)."""
        source = os.environ if env is None else env

        def read(key: str) -> str:
            value = source.get(key, _DEFAULTS.get(key, ""))
            return value.strip()

        raw_port = read("DATABASE_PORT") or _DEFAULTS["DATABASE_PORT"]
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ConfigurationError(f"DATABASE_PORT must be an integer, got {raw_port!r}") from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError(f"DATABASE_PORT out of range: {port}")

        name = read("DATABASE_NAME")
        user = read("DATABASE_USER")
        if not name:
            raise ConfigurationError("DATABASE_NAME is not set")
        if not user:
            raise ConfigurationError("DATABASE_USER is not set")

        raw_timeout = source.get("DATABASE_STATEMENT_TIMEOUT_MS", "").strip()
        statement_timeout_ms = int(raw_timeout) if raw_timeout else 30_000
        if statement_timeout_ms < 0:
            raise ConfigurationError("DATABASE_STATEMENT_TIMEOUT_MS must be >= 0")

        return cls(
            host=read("DATABASE_HOST") or _DEFAULTS["DATABASE_HOST"],
            port=port,
            name=name,
            user=user,
            password=Secret(source.get("DATABASE_PASSWORD", _DEFAULTS["DATABASE_PASSWORD"])),
            statement_timeout_ms=statement_timeout_ms,
            application_name=source.get("DATABASE_APP_NAME", "erp-backend").strip()
            or "erp-backend",
        )

    def dsn(self, *, driver: str = "postgresql+psycopg") -> str:
        """
        Build a SQLAlchemy URL.

        The password is percent-encoded so that special characters cannot break
        the URL. The returned string contains the secret and must therefore
        never be logged (§10) — use `safe_dsn` for diagnostics.
        """
        revealed = self.password.reveal()
        password = f":{quote(revealed, safe='')}" if revealed else ""
        return (
            f"{driver}://{quote(self.user, safe='')}{password}@{self.host}:{self.port}/{self.name}"
        )

    def safe_dsn(self, *, driver: str = "postgresql+psycopg") -> str:
        """Same as :meth:`dsn` but with the password masked — safe to log (§10)."""
        password = ":***" if self.password else ""
        return (
            f"{driver}://{quote(self.user, safe='')}{password}@{self.host}:{self.port}/{self.name}"
        )

    def connect_args(self) -> dict[str, str]:
        """psycopg connection options: UTC session, statement timeout (§15)."""
        options = f"-c timezone=UTC -c statement_timeout={self.statement_timeout_ms}"
        return {
            "application_name": self.application_name,
            "options": options,
        }


def get_database_settings(env: dict[str, str] | None = None) -> DatabaseSettings:
    """Convenience wrapper used by Alembic, tests and the engine factory."""
    return DatabaseSettings.from_env(env)


__all__ = [
    "ConfigurationError",
    "DatabaseSettings",
    "Secret",
    "get_database_settings",
]
