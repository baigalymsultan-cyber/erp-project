"""TD-016 P4 — the single HTTP boundary between the Streamlit UI and the API.

Every UI data path goes through this client; no UI module touches SQLAlchemy,
``session_scope`` or SQL directly any more (REQ-TD016-04, ADR-014).

Transport:
  * ``ERP_API_BASE_URL`` set  → real HTTP (the documented production layout:
    Streamlit and uvicorn are separate processes, §12);
  * otherwise                 → in-process ASGI transport (the UI and the API
    live in the same interpreter — zero-config local/dev; the boundary, the
    authorisation and the audit trail are the production ones regardless of
    transport, which is what makes the boundary testable in CI).

The JWT lives in ``st.session_state`` only; it is never written to disk.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

_TOKEN_KEY = "auth_token"

#: Shared app-instance for the in-process transport (resolved once).
_app: Any = None


def _app_instance() -> Any:
    global _app
    if _app is None:
        from app.api import app as fastapi_app

        _app = fastapi_app
    return _app


def base_url() -> str:
    return os.environ.get("ERP_API_BASE_URL", "").rstrip("/")


def _new_client(token: str | None) -> Any:
    """A client bound to the token's auth header.

    * ERP_API_BASE_URL set → real httpx (production: separate processes);
    * otherwise → starlette TestClient (in-process ASGI: dev/CI, same
      authorisation and audit trail as the production transport).
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = base_url()
    if url:
        return httpx.Client(base_url=url, headers=headers, timeout=15.0)
    from starlette.testclient import TestClient

    client: TestClient = TestClient(_app_instance())
    client.headers.update(headers)
    return client


def store_token(token: str) -> None:
    st.session_state[_TOKEN_KEY] = token


def clear_token() -> None:
    st.session_state.pop(_TOKEN_KEY, None)


def current_token() -> str | None:
    value = st.session_state.get(_TOKEN_KEY)
    return value if isinstance(value, str) else None


class ApiError(RuntimeError):
    """A non-2xx API response; ``kind`` drives uniform UI messages."""

    def __init__(self, status_code: int, kind: str) -> None:
        super().__init__(f"API {status_code}: {kind}")
        self.status_code = status_code
        self.kind = kind


def request(method: str, path: str, token: str | None = None, **kwargs: Any) -> Any:
    """One API call; raises :class:`ApiError` on 4xx/5xx, returns parsed JSON."""
    with _new_client(token) as client:
        response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise ApiError(response.status_code, response.text[:200])
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def get(path: str, token: str | None = None, **params: Any) -> Any:
    return request("GET", path, token=token, params=params)
