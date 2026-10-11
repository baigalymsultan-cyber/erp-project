"""
SQLAlchemy engine and session factory.

MASTER PROMPT references:
  §7  — SQLAlchemy 2.x;
  §15 — every connection works in UTC;
  §54 — audit rows must carry the acting user: the session variable
        app.current_user_id is read by the database trigger fn_audit(),
        so the application sets it per transaction instead of duplicating
        audit logic in Python.

The engine is created lazily so that importing this module never opens a
connection (required for Alembic offline mode and for unit tests that do not
touch the database).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import DatabaseSettings, get_database_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def create_database_engine(settings: DatabaseSettings | None = None) -> Engine:
    """Create a new Engine. Callers are responsible for disposing of it."""
    resolved = settings or get_database_settings()
    engine = create_engine(
        resolved.dsn(),
        connect_args=resolved.connect_args(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
        future=True,
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _set_session_defaults(dbapi_connection, connection_record) -> None:
        """Force UTC on every physical connection (§15)."""
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")

    return engine


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_database_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory, creating it on first use."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )
    return _session_factory


def dispose_engine() -> None:
    """Dispose of the process-wide engine (used by tests and shutdown hooks)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def set_current_user(session: Session, user_id: int | None) -> None:
    """
    Publish the acting user to the database session (§54).

    The value is read by fn_current_app_user() inside fn_audit(); when it is
    not set, audit rows are written with user_id = NULL rather than a faked
    identity (§54).

    set_config(..., TRUE) makes the setting transaction-local, so it cannot
    leak to another request through a pooled connection.
    """
    session.execute(
        text("SELECT set_config('app.current_user_id', :value, TRUE)"),
        {"value": "" if user_id is None else str(int(user_id))},
    )


@contextmanager
def session_scope(user_id: int | None = None) -> Iterator[Session]:
    """
    Transactional session scope.

    Commits on success, rolls back on any exception, always closes the session.
    """
    session = get_session_factory()()
    try:
        if user_id is not None:
            set_current_user(session, user_id)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "create_database_engine",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "session_scope",
    "set_current_user",
]
