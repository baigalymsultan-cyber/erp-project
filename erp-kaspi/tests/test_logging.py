"""
§71 — structured logging behaviour: one JSON line, context stamping,
secret masking in any nesting level. No secrets ever reach the output.
"""

from __future__ import annotations

import io
import json
import logging

from sqlalchemy import text

from app.core.logging import JsonFormatter, bind_request_context, mask_secrets


def _captured(logger_name: str = "probe") -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger, stream


def test_record_is_one_json_line_with_context():
    bind_request_context(request_id="req-42", user_id=7, operation_id=99)
    logger, stream = _captured()
    logger.info("order saved")
    entry = json.loads(stream.getvalue())
    assert entry["level"] == "INFO"
    assert entry["module"].endswith("probe")
    assert entry["message"] == "order saved"
    assert entry["request_id"] == "req-42"
    assert entry["user_id"] == 7
    assert entry["operation_id"] == 99


def test_secrets_are_masked_at_any_depth():
    bind_request_context()
    logger, stream = _captured()
    logger.info(
        "payload",
        extra={
            "password": "hunter2",
            "kaspi_token": "t0k3n",
            "nested": {"api_key": "k", "amount": "100.00"},
            "items": [{"authorization": "Bearer x"}, {"qty": 2}],
        },
    )
    entry = json.loads(stream.getvalue())["fields"]
    assert entry["password"] == "***"
    assert entry["kaspi_token"] == "***"
    assert entry["nested"] == {"api_key": "***", "amount": "100.00"}
    assert entry["items"][0] == {"authorization": "***"}
    assert entry["items"][1] == {"qty": 2}


def test_mask_secrets_is_recursive_helper():
    data = {"secret": 1, "ok": [{"encrypted_token": "e"}]}
    assert mask_secrets(data) == {"secret": "***", "ok": [{"encrypted_token": "***"}]}


def test_dsn_password_is_masked_in_free_text():
    """ARCH-REVIEW MEDIUM — driver errors can carry a full DSN in free text."""
    from app.core.logging import sanitize_text

    masked = sanitize_text(
        "connect failed: postgresql://erp_app:s3cr3t@127.0.0.1:5432/erp"
    )
    assert masked == "connect failed: postgresql://erp_app:***@127.0.0.1:5432/erp"
    assert "s3cr3t" not in masked
    # no password in the DSN — must stay untouched
    untouched = sanitize_text("postgresql://erp_app@127.0.0.1:5432/erp")
    assert untouched == "postgresql://erp_app@127.0.0.1:5432/erp"


def test_dsn_password_variants_and_negatives():
    """postgres:// scheme, ports, encoded chars masked; safe parts intact."""
    from app.core.logging import sanitize_text

    # postgres:// (short scheme) with port
    out = sanitize_text("postgres://u:p%40ss@db:5432/x")
    assert out == "postgres://u:***@db:5432/x"
    assert "p%40ss" not in out
    # special chars in password
    out = sanitize_text("dial postgres://admin:s3cr3t!@host/db now")
    assert out == "dial postgres://admin:***@host/db now"
    assert "s3cr3t!" not in out
    # uppercase scheme
    out = sanitize_text("POSTGRESQL://u:pw@h/d")
    assert out == "POSTGRESQL://u:***@h/d"
    # userinfo without scheme — not a DSN, must stay untouched
    assert sanitize_text("user:pass@host") == "user:pass@host"
    # plain text without secrets — untouched
    assert sanitize_text("all systems nominal") == "all systems nominal"
    # existing KV/Bearer masking still works (no regression)
    assert sanitize_text("password=hunter2") == "password=***"
    assert sanitize_text("Authorization: Bearer abc123") == "Authorization: Bearer ***"


def test_authenticated_request_logs_user_id(engine, applied_schema):
    """§71 — once the principal is resolved, user_id rides every log line."""
    from fastapi.testclient import TestClient

    from app.api import app
    from app.core.security import hash_password, reset_auth_rate_limit

    reset_auth_rate_limit()

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash) VALUES (:u, :h) "
                "ON CONFLICT (username) DO NOTHING"
            ),
            {"u": "logger-user", "h": hash_password("Logger-Pass-2026")},
        )
        conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT u.id, r.id FROM users u, roles r "
                "WHERE u.username = 'logger-user' AND r.code = 'VIEWER' "
                "ON CONFLICT DO NOTHING"
            )
        )

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    api_logger = logging.getLogger("erp.api")
    api_logger.addHandler(handler)
    try:
        with TestClient(app) as client:
            r = client.post(
                "/auth/login",
                json={"username": "logger-user", "password": "Logger-Pass-2026"},
            )
            assert r.status_code == 200, r.text
            assert (
                client.get(
                    "/pnl", headers={"Authorization": f"Bearer {r.json()['access_token']}"}
                ).status_code
                == 200
            )
    finally:
        api_logger.removeHandler(handler)

    request_records = [rec for rec in records if rec.getMessage() == "request"]
    assert request_records
    entry = json.loads(JsonFormatter().format(request_records[-1]))
    assert isinstance(entry["user_id"], int), entry


def test_request_middleware_stamps_request_id_and_logs():
    """§71 — every API request carries a request_id in its log line."""
    from fastapi.testclient import TestClient

    from app.api import app

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    api_logger = logging.getLogger("erp.api")
    api_logger.addHandler(handler)
    try:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
    finally:
        api_logger.removeHandler(handler)

    request_records = [r for r in records if r.getMessage() == "request"]
    assert request_records, "the middleware must log one line per request"
    formatted = JsonFormatter().format(request_records[-1])
    entry = json.loads(formatted)
    assert entry["request_id"], "request_id must be set for every request"
    assert entry["fields"]["method"] == "GET"
    assert entry["fields"]["path"] == "/health"
    assert entry["fields"]["status_code"] == 200
