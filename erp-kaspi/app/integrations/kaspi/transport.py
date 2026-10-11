"""Abstract transport for the Kaspi clients (§42; OQ-003).

The real HTTP transport is written only together with the verified Kaspi
API specification (§94 — endpoint paths, auth headers and pagination
shape are NOT invented). Today this module provides:

* `Transport` — the abstract interface every client depends on;
* `MockTransport` — in-memory implementation for tests;
* `RetryingTransport` — tenacity-style exponential backoff, permanent
  errors never retried, optional minimum call interval (rate limiting);
* request/response value types.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TransportRequest:
    request_type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransportResponse:
    request_type: str
    body: dict[str, Any]
    external_id: str | None = None


class TransportError(Exception):
    """Any transport-level failure (network, 5xx, timeout)."""


class TransportPermanentError(TransportError):
    """A failure that must never be retried (4xx, bad credentials)."""


class Transport:
    """Abstract transport — the seam where the real HTTP client will land."""

    def call(self, request: TransportRequest) -> TransportResponse:
        raise NotImplementedError


class MockTransport(Transport):
    """In-memory transport for tests: scripted responses and failures."""

    def __init__(
        self,
        responses: dict[str, TransportResponse] | None = None,
        *,
        fail_times: dict[str, int] | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.fail_times = dict(fail_times or {})
        self.calls: list[TransportRequest] = []
        self._failures_left = dict(self.fail_times)

    def call(self, request: TransportRequest) -> TransportResponse:
        self.calls.append(request)
        left = self._failures_left.get(request.request_type, 0)
        if left > 0:
            self._failures_left[request.request_type] = left - 1
            scripted = self.responses.get(request.request_type)
            if isinstance(scripted, TransportPermanentError):
                raise TransportPermanentError(str(scripted))
            raise TransportError(f"transient failure for {request.request_type!r}")
        response = self.responses.get(request.request_type)
        if response is None:
            raise TransportPermanentError(f"no scripted response for {request.request_type!r}")
        if isinstance(response, TransportPermanentError):
            raise TransportPermanentError(str(response))
        return response


class RetryingTransport(Transport):
    """Wraps a transport with bounded retries and rate limiting.

    Retry policy: exponential backoff (base * 2**attempt, capped), only
    for transient `TransportError`; `TransportPermanentError` is raised
    immediately (a 4xx retry would be a bug, §67).
    """

    def __init__(
        self,
        inner: Transport,
        *,
        max_attempts: int = 4,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        min_interval_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.max_attempts = max(1, max_attempts)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.min_interval_seconds = min_interval_seconds
        self._sleep = sleep
        self._clock = clock
        self._last_call_at: float | None = None

    def call(self, request: TransportRequest) -> TransportResponse:
        last_exc: TransportError | None = None
        for attempt in range(self.max_attempts):
            self._respect_rate_limit()
            try:
                response = self.inner.call(request)
                self._last_call_at = self._clock()
                return response
            except TransportPermanentError:
                raise
            except TransportError as exc:
                last_exc = exc
                delay = min(self.base_delay * (2**attempt), self.max_delay)
                self._sleep(delay)
        raise last_exc if last_exc else TransportError("transport failed")

    def _respect_rate_limit(self) -> None:
        if self.min_interval_seconds <= 0 or self._last_call_at is None:
            return
        elapsed = self._clock() - self._last_call_at
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            self._sleep(remaining)
        self._last_call_at = self._clock()
