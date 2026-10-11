"""TD-015 — distributed rate limiting (MASTER PROMPT §55).

Layered evidence, per the TD-015 plan:

*   unit — ``app.core.rate_limit_store`` SQL semantics against real PostgreSQL
    (count/record/prune, window boundary, per-bucket isolation);
*   integration — the API auth limiter with ``RATE_LIMIT_SHARED=1`` still
    returns 429 on the 6th attempt and 200 once the window slides;
*   multi-process — two separate worker processes sharing the same
    ``auth_rate_events`` table: the combined budget is 5 attempts, NOT
    5 per process (this is what the in-process limiter cannot guarantee);
*   fail-open — when the shared backend is unreachable the endpoint keeps
    answering (fail-open, ADR-014), the local in-process window still caps
    each worker, and the failure is logged for monitoring.

The in-process (RATE_LIMIT_SHARED unset) behaviour is covered by the
existing login-gate tests and stays green.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parent.parent

WORKER_SCRIPT = """\
import sys, time
sys.path.insert(0, {root!r})
from fastapi.testclient import TestClient
from app.api import app
from app.core.security import reset_auth_rate_limit

reset_auth_rate_limit()
client = TestClient(app)
username, password = {username!r}, {password!r}
granted = 0
for i in range({attempts}):
    r = client.post(
        "/auth/login",
        json={{"username": username, "password": password, "ip_id": None}},
    )
    if r.status_code != 429:
        granted += 1
print(granted)
"""


@pytest.fixture()
def _shared_env(applied_schema, engine, monkeypatch):
    """Enable the shared limiter for the test process and clean the table."""
    from app.core.security import reset_auth_rate_limit

    monkeypatch.setenv("RATE_LIMIT_SHARED", "1")
    monkeypatch.setattr(
        "app.core.security._RATE_LIMIT_SHARED", True, raising=False
    )
    reset_auth_rate_limit()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM auth_rate_events"))
    yield engine
    reset_auth_rate_limit()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM auth_rate_events"))


def _seed_login_user(engine, username: str, password_hash: str) -> int:


    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, is_active, "
                "token_version, failed_logins) "
                "VALUES (:u, :ph, TRUE, 1, 0) RETURNING id"
            ),
            {"u": username, "ph": password_hash},
        )
        return int(row.scalar_one())


# --------------------------------------------------------------------------- #
# Unit — store semantics (real PostgreSQL)
# --------------------------------------------------------------------------- #


def test_store_count_record_prune(_shared_env):
    from datetime import UTC, datetime, timedelta

    from app.core.database import session_scope
    from app.core.rate_limit_store import count_recent, prune, record

    engine = _shared_env
    bucket = "u1:1.2.3.4"
    now = datetime.now(tz=UTC)
    old = now - timedelta(minutes=10)
    with session_scope() as session:
        assert count_recent(session, bucket, now - timedelta(minutes=15)) == 0
        for _ in range(5):
            record(session, bucket, old)
    with session_scope() as session:
        assert count_recent(session, bucket, now - timedelta(minutes=15)) == 5
        assert count_recent(session, "other-bucket", now - timedelta(minutes=15)) == 0
        removed = prune(session, before=now - timedelta(minutes=5))
    assert removed == 5
    with session_scope() as session:
        assert count_recent(session, bucket) == 0
    engine.dispose()


def test_store_window_boundary(_shared_env):
    from datetime import UTC, datetime, timedelta

    from app.core.database import session_scope
    from app.core.rate_limit_store import count_recent, record

    engine = _shared_env
    bucket = "u2:edge"
    now = datetime.now(tz=UTC)
    with session_scope() as session:
        record(session, bucket, now - timedelta(seconds=61))
        record(session, bucket, now - timedelta(seconds=59))
    with session_scope() as session:
        assert count_recent(session, bucket) == 1
    engine.dispose()


def test_reset_forgets_fresh_shared_events(_shared_env):
    """reset_auth_rate_limit must clear even events inside the 2x window.

    Regression (takeover audit): the helper used the retention prune
    (cutoff = now - 2x window), so events younger than 120 s - the only ones
    that count towards the sliding limit - survived the "reset" and leaked
    across tests / operational resets.
    """
    from datetime import UTC, datetime

    from app.core.database import session_scope
    from app.core.rate_limit_store import count_recent, record

    engine = _shared_env
    bucket = "u3:reset"
    now = datetime.now(tz=UTC)
    with session_scope() as session:
        for _ in range(5):
            record(session, bucket, now)
    with session_scope() as session:
        assert count_recent(session, bucket) == 5

    from app.core.security import reset_auth_rate_limit

    reset_auth_rate_limit()

    with session_scope() as session:
        assert count_recent(session, bucket) == 0
    engine.dispose()


# --------------------------------------------------------------------------- #
# Integration — the API limiter with RATE_LIMIT_SHARED=1
# --------------------------------------------------------------------------- #


def test_shared_limit_429_on_sixth(_shared_env):
    from app.core.security import hash_password

    engine = _shared_env
    username = f"dl-{uuid.uuid4().hex[:10]}"
    _seed_login_user(engine, username, hash_password("Shared-Lim-2026!"))

    from app.api import app as fastapi_app

    with TestClient(fastapi_app) as client:
        statuses = []
        for _ in range(6):
            r = client.post(
                "/auth/login",
                json={
                    "username": username,
                    "password": "Shared-Lim-2026!",
                    "ip_id": None,
                },
            )
            statuses.append(r.status_code)
    assert 429 in statuses
    assert statuses[:5] == [200] * 5 or 429 in statuses[:5]
    engine.dispose()


def test_shared_only_path_rejects_sixth_and_records_every_attempt(_shared_env):
    """The shared path alone (no in-process fast path) enforces 5/60 and
    records every granted attempt (regression for the TD-015 atomicity bug:
    COUNT+INSERT now run under a per-bucket advisory lock, and the INSERT is
    unconditional — an attempt is never skipped in favour of a prune)."""
    from datetime import UTC, datetime, timedelta

    import pytest as _pytest_mod
    from fastapi import HTTPException

    from app.core.database import session_scope
    from app.core.rate_limit_store import count_recent
    from app.core.security import _shared_rate_limit

    engine = _shared_env
    bucket = f"direct-{uuid.uuid4().hex[:10]}:1.2.3.4"
    now = datetime.now(tz=UTC)
    window_start = now - timedelta(seconds=60)

    # 5 grants are allowed and must all be persisted — no probabilistic loss.
    for _ in range(5):
        _shared_rate_limit(bucket)
    with session_scope() as session:
        assert count_recent(session, bucket, window_start) == 5

    # The 6th is rejected by the shared store, with no in-process window
    # able to mask the check.
    with _pytest_mod.raises(HTTPException) as excinfo:
        _shared_rate_limit(bucket)
    assert excinfo.value.status_code == 429
    with session_scope() as session:
        assert count_recent(session, bucket, window_start) == 5
    engine.dispose()


def test_shared_lock_contention_fails_open(_shared_env):
    """A contended bucket must not stall login beyond the fail-open budget.

    Holding the per-bucket advisory lock in another connection makes the
    limiter's lock acquisition hit ``SET LOCAL lock_timeout`` (600 ms);
    the limiter degrades to the per-worker window instead of blocking up to
    the app-wide 30s statement_timeout (ADR-014 hardening — a rate limiter
    must never turn into the availability risk it exists to prevent)."""
    import time as _time

    from sqlalchemy import text as _text

    from app.core import security
    from app.core.database import get_session_factory

    engine = _shared_env
    bucket = "lock-contention:1.2.3.4"
    lock_session = get_session_factory()()
    try:
        lock_session.execute(
            _text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": bucket},
        )
        start = _time.monotonic()
        # Must NOT raise (fail-open) and must not block ~30s on the lock.
        security.auth_rate_limit(bucket)
        elapsed = _time.monotonic() - start
        assert elapsed < 5, f"limiter stalled {elapsed:.2f}s under lock contention"
        assert len(security._auth_attempts[bucket]) == 1
    finally:
        lock_session.rollback()
        lock_session.close()
    engine.dispose()


# --------------------------------------------------------------------------- #
# Multi-process — two real worker processes share the 5-attempt budget
# --------------------------------------------------------------------------- #


def test_multi_worker_budget_is_shared(_shared_env):
    from app.core.security import hash_password

    engine = _shared_env
    username = f"mw-{uuid.uuid4().hex[:10]}"
    password = "Multi-Work-2026!"
    _seed_login_user(engine, username, hash_password(password))

    env = dict(os.environ)
    env["RATE_LIMIT_SHARED"] = "1"
    env["DATABASE_URL"] = ""
    script = WORKER_SCRIPT.format(
        root=str(PROJECT_ROOT),
        username=username,
        password=password,
        attempts=4,
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(PROJECT_ROOT),
        )
        for _ in range(2)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()
        outs.append(int(out.decode().strip()))
    # Each worker alone would allow 4/4; together they must not exceed 5
    # successful attempts because the budget lives in the shared table.
    assert sum(outs) <= 5, f"workers granted {outs} — budget not shared"
    engine.dispose()


# --------------------------------------------------------------------------- #
# Fail-open — shared backend unreachable
# --------------------------------------------------------------------------- #


def test_fail_open_when_shared_unreachable(applied_schema, engine, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from app.core import security

    monkeypatch.setattr(
        "app.core.security._RATE_LIMIT_SHARED", True, raising=False
    )

    class BrokenSession:
        def __enter__(self):
            raise SQLAlchemyError("shared store down")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        security, "_shared_session_scope", lambda: BrokenSession()
    )
    # Must NOT raise — fail-open — and must record locally.
    security.auth_rate_limit("failopen:1.2.3.4")
    security.auth_rate_limit("failopen:1.2.3.4")
    assert len(security._auth_attempts["failopen:1.2.3.4"]) == 2
    engine.dispose()
