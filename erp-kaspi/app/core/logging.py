"""
§71 — Structured logging for the ERP backend.

One JSON object per line on stderr: timestamp, level, module, request ID,
user ID, operation ID, message, plus optional structured fields. Secrets are
never serialised: keys whose name matches a secret pattern are masked
(TD-002 lesson — the pattern list lives in one place and is checked by test).

Request context (request id / user id / operation id) travels through a
``contextvars.ContextVar`` set by the API middleware / service code; the
formatter reads it at emit time, so concurrent requests never mix contexts.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar

#: Context values for one logical request (§71).
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_ctx: ContextVar[int | None] = ContextVar("user_id", default=None)
operation_id_ctx: ContextVar[int | None] = ContextVar("operation_id", default=None)

#: Secret detection (§71 «НЕ логировать»): any key containing these stems is
#: masked wherever it appears — top level or nested. TD-002 asks for a CI check
#: that new secret-ish columns are reflected here; test_logging.py asserts the
#: behaviour end-to-end.
_SECRET_STEMS = (
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "encrypted_token",
)
_SECRET_KEY_RE = re.compile("|".join(_SECRET_STEMS), re.IGNORECASE)
_MASK = "***"

#: Keys that carry meaning for the ERP and are always serialised if present.
_CONTEXT_KEYS = ("request_id", "user_id", "operation_id")


def mask_secrets(value: object) -> object:
    """Recursively mask secret-looking keys in dicts/lists (§71)."""
    if isinstance(value, dict):
        return {
            key: _MASK if _SECRET_KEY_RE.search(str(key)) else mask_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [mask_secrets(item) for item in value]
    return value


#: §11 — secret values in free text (exception messages, SQL errors, URLs).
#: Matches ``key=value``, ``key: value``, ``"key": "value"`` and
#: ``Bearer <token>`` shapes; the value is replaced, the key is kept so the
#: log stays useful for debugging.
_SECRET_VALUE_RE = re.compile(
    r"(?i)([\"']?)(password|passwd|pwd|token|secret|api[_-]?key)([\"']?)"
    r"(\s*[:=]\s*)([\"']?)([^\s\"',;}]+)\5"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9\-._~+/=]+)")

#: DSN shape ``scheme://user:password@host`` — the KV patterns above do not
#: fire here (no secret keyword), but driver errors and middleware tracebacks
#: can carry a full DSN with the real password (ARCH-REVIEW MEDIUM finding).
_DSN_PASSWORD_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)"
)


def sanitize_text(text: str) -> str:
    """Mask secret values in free text (§11).

    Exception tracebacks and driver errors can carry connection strings,
    tokens or passwords. This keeps the key name (useful for debugging)
    but replaces the value.
    """
    def _mask_kv(m: re.Match[str]) -> str:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}{m.group(5)}{_MASK}{m.group(5)}"

    text = _DSN_PASSWORD_RE.sub(lambda m: f"{m.group(1)}{_MASK}{m.group(3)}", text)
    text = _SECRET_VALUE_RE.sub(_mask_kv, text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {_MASK}", text)
    return text


class JsonFormatter(logging.Formatter):
    """Serialise every record as one JSON line (§71)."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "module": record.name,
            # §11 — the message itself can carry secrets (e.g. driver errors).
            "message": sanitize_text(record.getMessage()),
        }
        # The context travels in ContextVars; when a record is formatted in a
        # different context than it was emitted (a test capturing records
        # across threads), fall back to the extra the emitter attached.
        request_id = request_id_ctx.get()
        if request_id is None:
            request_id = record.__dict__.get("request_id")
        if request_id is not None:
            entry["request_id"] = request_id
        user_id = user_id_ctx.get()
        if user_id is None:
            user_id = record.__dict__.get("user_id")
        if user_id is not None:
            entry["user_id"] = user_id
        operation_id = operation_id_ctx.get()
        if operation_id is None:
            operation_id = record.__dict__.get("operation_id")
        if operation_id is not None:
            entry["operation_id"] = operation_id

        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in logging.LogRecord.__dict__ and key not in _CONTEXT_KEYS
        }
        if extra:
            entry["fields"] = mask_secrets(extra)
        if record.exc_info and record.exc_info[0] is not None:
            # §11 — tracebacks can leak secrets (connection strings, tokens).
            entry["exc_info"] = sanitize_text(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger exactly once."""
    root = logging.getLogger()
    if any(isinstance(handler, logging.StreamHandler) for handler in root.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def bind_request_context(
    request_id: str | None = None,
    user_id: int | None = None,
    operation_id: int | None = None,
) -> None:
    """Set the context values the formatter stamps into every record."""
    if request_id is not None:
        request_id_ctx.set(request_id)
    if user_id is not None:
        user_id_ctx.set(user_id)
    if operation_id is not None:
        operation_id_ctx.set(operation_id)
