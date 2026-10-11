"""Shared rate-limit backends for the auth limiter (TD-015, MASTER PROMPT §55).

The in-process sliding-window limiter (``_auth_attempts`` in
``app.core.security``) is only correct for a single worker process. In
production the API runs as several workers/replicas, so per-process memory
lets an attacker multiply the attempt budget by the worker count.

TD-015 introduces a shared counter in PostgreSQL:

*   one row per ``(bucket_key, event)`` in ``auth_rate_events``;
*   ``lock_bucket`` serialises the check+insert per bucket with a
    transaction-level advisory lock, so two workers can never both COUNT
    before the other's INSERT commits and both grant the 6th attempt;
*   ``count_recent`` counts rows in the sliding window;
*   ``record`` inserts one event row — called for every attempt;
*   ``prune`` deletes rows outside the window (called opportunistically
    with probability ``_SHARED_PRUNE_PROBABILITY``, in addition to the
    mandatory ``record``).

Fail-open policy (ADR-014): if the shared backend is unreachable the limiter
falls back to the in-process window rather than locking every user out of the
system. Availability beats strictness for an internal ERP; the failure is
logged so monitoring can page.

Local development keeps working with no extra infrastructure: the shared
backend is enabled only when ``RATE_LIMIT_SHARED=1`` (set by default in the
Docker/production compose; unset in dev).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

#: Window length must mirror _AUTH_WINDOW_SECONDS in app.core.security.
_WINDOW_SECONDS = 60

#: Rows older than the window are garbage; keep a safety margin (§9 memory).
_PRUNE_AFTER = timedelta(seconds=_WINDOW_SECONDS * 2)


def lock_bucket(session, bucket_key: str) -> None:
    """Serialize per-bucket check+insert across processes.

    A transaction-level advisory lock keyed on the bucket hash makes the
    COUNT-then-INSERT sequence atomic: without it two concurrent workers
    can both COUNT before the other's INSERT commits and both grant the
    6th attempt (observed in the multi-worker test as granted [3, 3]).
    Released automatically at transaction end.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": bucket_key},
    )


def count_recent(session, bucket_key: str, window_start: datetime | None = None) -> int:
    """Number of attempts for ``bucket_key`` inside the sliding window.

    ``window_start`` defaults to now minus the window; passing it explicitly
    makes tests deterministic and keeps the compare against the same clock
    the caller used for ``record``.
    """
    start = window_start or (datetime.now(UTC) - timedelta(seconds=_WINDOW_SECONDS))
    row = session.execute(
        text(
            "SELECT count(*) FROM auth_rate_events "
            "WHERE bucket_key = :k "
            "AND occurred_at > :start"
        ),
        {"k": bucket_key, "start": start},
    )
    return int(row.scalar_one())


def record(session, bucket_key: str, occurred_at: datetime) -> None:
    """Persist one attempt. ``occurred_at`` must be timezone-aware UTC."""
    session.execute(
        text(
            "INSERT INTO auth_rate_events (bucket_key, occurred_at) "
            "VALUES (:k, :t)"
        ),
        {"k": bucket_key, "t": occurred_at},
    )


def prune(session, before: datetime | None = None) -> int:
    """Delete events older than ``before`` (default: twice the window);
    returns rows removed."""
    cutoff = before or (datetime.now(UTC) - _PRUNE_AFTER)
    result = session.execute(
        text("DELETE FROM auth_rate_events WHERE occurred_at < :c"),
        {"c": cutoff},
    )
    return int(result.rowcount or 0)


def clear_all(session) -> int:
    """Delete every event regardless of age; returns rows removed.

    Unconditional wipe used by the ``reset_auth_rate_limit`` helper: unlike
    :func:`prune` it must also drop events inside the retention horizon —
    otherwise a just-used bucket survives the reset and leaks between
    tests / operational clears.
    """
    result = session.execute(text("DELETE FROM auth_rate_events"))
    return int(result.rowcount or 0)


def is_persistent_error(exc: BaseException) -> bool:
    """True when ``exc`` means the shared backend is unusable (fail-open)."""
    return isinstance(exc, SQLAlchemyError)
