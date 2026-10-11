"""Kaspi Shop / Pay client skeletons (§42; OQ-003).

No endpoint paths or payload shapes are invented (§94). A client run is
one `sync_log` row (§45): pages are fetched through the abstract
transport, every response body is stored as a raw JSONB payload (§26)
deduplicated by SHA-256 (§41), and counters drive the sync status:

* all pages OK                     -> SUCCESS
* some pages OK, some failed       -> PARTIAL
* every page failed                -> FAILED

Dry-run is the caller's decision (rollback the session, §38).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.kaspi.transport import Transport, TransportError, TransportResponse
from app.models.kaspi import RawKaspiPayload, SyncLog

log = logging.getLogger(__name__)  # payloads/secrets never logged (§71)


def payload_hash(body: dict) -> str:
    """SHA-256 of the canonical JSON — dict order is irrelevant (§41)."""
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class SyncResult:
    sync_log_id: int
    status: str
    fetched: int
    inserted: int
    updated: int
    errors: int


_ALLOWED_API_TYPES = ("SHOP", "PAY")


class BaseKaspiClient:
    """Shared sync engine for the Shop and Pay clients (§42)."""

    api_type: str = ""

    def __init__(self, transport: Transport, session: Session, ip_id: int) -> None:
        if self.api_type not in _ALLOWED_API_TYPES:
            raise ValueError(f"§42: api_type must be one of {_ALLOWED_API_TYPES}")
        self.transport = transport
        self.session = session
        self.ip_id = ip_id

    # -- raw payload storage (§26, §41) ------------------------------------ #

    def store_raw_payload(self, response: TransportResponse) -> RawKaspiPayload | None:
        """Store the response body once; identical bodies are skipped.

        Returns the stored row, or None when the payload was already
        stored (same ip + request_type + hash).
        """
        digest = payload_hash(response.body)
        existing = self.session.scalar(
            select(RawKaspiPayload).where(
                RawKaspiPayload.ip_id == self.ip_id,
                RawKaspiPayload.request_type == response.request_type,
                RawKaspiPayload.payload_hash == digest,
            )
        )
        if existing is not None:
            return None
        row = RawKaspiPayload(
            ip_id=self.ip_id,
            request_type=response.request_type,
            api_type=self.api_type,
            external_id=response.external_id,
            payload=response.body,
            payload_hash=digest,
        )
        self.session.add(row)
        self.session.flush()
        return row

    # -- sync lifecycle (§45) ---------------------------------------------- #

    def run_sync(
        self,
        request_type: str,
        *,
        pages: list[dict] | None = None,
        period_from=None,
        period_to=None,
        request_builder=None,
    ) -> SyncResult:
        """Fetch every page through the transport; one page failure is isolated.

        `request_builder(page_payload)` lets the (future, spec-verified)
        client map a page descriptor to the real wire payload; the
        default passes the descriptor through unchanged.
        """
        build = request_builder or (lambda page: page)
        log.info(
            "kaspi sync start: ip=%s api=%s type=%s pages=%d",
            self.ip_id,
            self.api_type,
            request_type,
            len(pages or []),
        )
        sync_log = SyncLog(
            ip_id=self.ip_id,
            api_type=self.api_type,
            status="RUNNING",
            period_from=period_from,
            period_to=period_to,
        )
        self.session.add(sync_log)
        self.session.flush()

        fetched = inserted = updated = errors = 0
        page_errors: list[dict] = []

        for page_index, page in enumerate(pages or []):
            try:
                response = self.transport.call(_build_request(request_type, build(page)))
            except TransportError as exc:
                errors += 1
                page_errors.append({"page": str(page_index), "error": str(exc)})
                log.warning(
                    "kaspi sync page failed: ip=%s api=%s type=%s page=%d",
                    self.ip_id,
                    self.api_type,
                    request_type,
                    page_index,
                )
                continue
            fetched += 1
            stored = self.store_raw_payload(
                TransportResponse(
                    request_type,
                    response.body,
                    external_id=response.external_id,
                )
            )
            if stored is not None:
                inserted += 1
            else:
                updated += 1

        sync_log.records_fetched = fetched
        sync_log.records_inserted = inserted
        sync_log.records_updated = updated
        sync_log.errors_count = errors
        # §45 — the sync has finished in every handled case (SUCCESS, PARTIAL,
        # FAILED); only an unhandled crash may leave sync_finished_at NULL.
        sync_log.sync_finished_at = func.now()
        if page_errors:
            sync_log.error_details = {"errors": page_errors[:50]}
        sync_log.status = _sync_status(fetched, errors)
        self.session.flush()
        log.info(
            "kaspi sync done: id=%d status=%s fetched=%d ins=%d upd=%d err=%d",
            sync_log.id,
            sync_log.status,
            fetched,
            inserted,
            updated,
            errors,
        )

        return SyncResult(
            sync_log_id=sync_log.id,
            status=sync_log.status,
            fetched=fetched,
            inserted=inserted,
            updated=updated,
            errors=errors,
        )


def _build_request(request_type: str, payload: dict):
    from app.integrations.kaspi.transport import TransportRequest

    return TransportRequest(request_type, payload)


def _sync_status(fetched: int, errors: int) -> str:
    if errors == 0:
        return "SUCCESS"
    if fetched == 0:
        return "FAILED"
    return "PARTIAL"


class KaspiShopClient(BaseKaspiClient):
    """Kaspi Магазин (orders) — skeleton; endpoints pending the spec (OQ-003)."""

    api_type = "SHOP"


class KaspiPayClient(BaseKaspiClient):
    """Kaspi Pay (payments) — skeleton; endpoints pending the spec (OQ-003)."""

    api_type = "PAY"
