"""
FastAPI application — the HTTP face of the ERP backend (MASTER PROMPT §59).

Every route is real: it goes through the services (app/services) against the applied
PostgreSQL schema (database/schema.sql). No mocks, no invented endpoints.

RBAC (§53, OQ-011): every business endpoint enforces the role hierarchy in
the backend via ``require_privilege`` — reads need VIEWER+, ledger/finance
writes ACCOUNTANT+, orders SHOP_MANAGER+, foreman work entries BRIGADIER+.
The exact per-role permission matrix stays open on OQ-011; the hierarchy
thresholds are the conservative fallback.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import auth as auth_routes
from app.api import bank as bank_routes
from app.api import inventory as inventory_routes
from app.api import kaspi as kaspi_routes
from app.api import operations as operations_routes
from app.api import orders as orders_routes
from app.api import payroll as payroll_routes
from app.api import production as production_routes
from app.api import reference as reference_routes
from app.api import reports as reports_routes
from app.api import system as system_routes
from app.api import transfers as transfers_routes
from app.api.deps import APP_5XX, logger
from app.core.logging import bind_request_context, configure_logging

logger.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Graceful shutdown handler (P0-3).

    On startup: nothing special (DB connections are per-request).
    On shutdown: log that we're draining; in-flight requests complete
    via Uvicorn's graceful shutdown (SIGTERM → --timeout-graceful-shutdown).
    Clients should retry with Idempotency-Key (C-1/C-2) if disconnected.
    """
    configure_logging()
    yield


app = FastAPI(
    title="ERP / Finance / Kaspi / MRP — Kazakhstan",
    version="0.2.0",
    description=(
        "Kaspi Магазин + Kaspi Pay, финансовый учёт, заказы, мебельное "
        "производство, сдельная зарплата, себестоимость, аналитика."
    ),
    lifespan=lifespan,
)


class _RequestLogMiddleware:
    """\u00a771 \u2014 one JSON log line per request.

    Pure ASGI, not BaseHTTPMiddleware: ``current_principal`` stores the
    authenticated user id in ``scope["state"]`` and this middleware reads it
    from there after the response (a ContextVar set inside a threadpool
    dependency does not propagate back to this task).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex[:12]
        bind_request_context(request_id=request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append("x-request-id", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            APP_5XX.count += 1
            logger.exception(
                "request failed",
                extra={"method": scope.get("method", "?"), "path": scope.get("path", "?")},
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if status_code >= 500:
            APP_5XX.count += 1
        logger.info(
            "request",
            extra={
                "method": scope["method"],
                "path": scope["path"],
                "status_code": status_code,
                "duration_ms": duration_ms,
                "request_id": request_id,
                "user_id": (scope.get("state") or {}).get("user_id"),
            },
        )


app.add_middleware(_RequestLogMiddleware)


@app.exception_handler(ValueError)
def _value_error_handler(request, exc: ValueError) -> JSONResponse:
    """Domain errors (§27/§28/§32 checks) become 422, never 500."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})

# --------------------------------------------------------------------------- #
# Router registration — order matches the original monolith's route order
# as closely as domain grouping allows (no two domains share a path prefix,
# so cross-domain order does not affect routing).
# --------------------------------------------------------------------------- #

app.include_router(operations_routes.router)
app.include_router(system_routes.router)
app.include_router(orders_routes.router)
app.include_router(payroll_routes.router)
app.include_router(reference_routes.router)
app.include_router(kaspi_routes.router)
app.include_router(transfers_routes.router)
app.include_router(auth_routes.router)
app.include_router(bank_routes.router)
app.include_router(inventory_routes.router)
app.include_router(production_routes.router)
app.include_router(reports_routes.router)
