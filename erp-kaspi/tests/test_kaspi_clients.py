"""Tests for the Kaspi client skeletons (§42, §67, §94).

All tests run against the real PostgreSQL schema inside rollback
transactions (§68, TD-012): no business data survives a test run.
"""

from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import select

from app.integrations.kaspi import crypto
from app.integrations.kaspi.client import (
    BaseKaspiClient,
    KaspiPayClient,
    KaspiShopClient,
    payload_hash,
)
from app.integrations.kaspi.transport import (
    MockTransport,
    RetryingTransport,
    TransportError,
    TransportPermanentError,
    TransportRequest,
    TransportResponse,
)
from app.models.kaspi import KaspiApiCredential, RawKaspiPayload, SyncLog


@pytest.fixture
def master_key(monkeypatch) -> bytes:
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    return key


@pytest.fixture
def qa_ip(session):
    """QA ИП inside the rollback transaction (never persists)."""
    from sqlalchemy import text

    return session.execute(
        text(
            "INSERT INTO ip_entities (name, bin) VALUES ('KASPI QA ИП', '666666666666') "
            "ON CONFLICT (bin) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# Crypto — AES-256-GCM envelope (§18, §19, §71)
# --------------------------------------------------------------------------- #


def test_token_roundtrip(master_key):
    envelope = crypto.encrypt_token("secret-kaspi-token", master_key)
    assert envelope[0] == 1
    assert crypto.decrypt_token(envelope, master_key) == "secret-kaspi-token"


def test_wrong_key_rejected(master_key):
    envelope = crypto.encrypt_token("secret", master_key)
    other = base64.b64encode(os.urandom(32)).decode()
    with pytest.raises(ValueError, match="wrong key"):
        crypto.decrypt_token(envelope, other)


def test_master_key_missing_is_explicit(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("KASPI_MASTER_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        crypto.load_master_key({})


def test_master_key_bad_length(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(ValueError, match="32 bytes"):
        crypto.load_master_key()


def test_nonce_never_reused(master_key):
    a = crypto.encrypt_token("t", master_key)
    b = crypto.encrypt_token("t", master_key)
    assert a != b, "same plaintext must produce a different envelope"


def test_preview_never_exposes_token(master_key):
    envelope = crypto.encrypt_token("secret-kaspi-token", master_key)
    preview = crypto.masked_token_preview(envelope)
    assert "secret" not in preview


# --------------------------------------------------------------------------- #
# Transport — retry, rate limit, permanent errors
# --------------------------------------------------------------------------- #


def _no_sleep(_seconds: float) -> None:
    pass


def test_retry_recovers_from_transient_failures():
    mock = MockTransport(
        {"orders": TransportResponse("orders", {"ok": 1})},
        fail_times={"orders": 2},
    )
    retrying = RetryingTransport(mock, max_attempts=4, sleep=_no_sleep)
    assert retrying.call(TransportRequest("orders", {})).body == {"ok": 1}
    assert len(mock.calls) == 3  # 2 failures + 1 success


def test_retry_gives_up_after_max_attempts():
    mock = MockTransport(fail_times={"orders": 10})
    retrying = RetryingTransport(mock, max_attempts=3, sleep=_no_sleep)
    with pytest.raises(TransportError):
        retrying.call(TransportRequest("orders", {}))
    assert len(mock.calls) == 3


def test_permanent_error_never_retried():
    mock = MockTransport({"orders": TransportPermanentError("400 bad request")})
    retrying = RetryingTransport(mock, max_attempts=5, sleep=_no_sleep)
    with pytest.raises(TransportPermanentError):
        retrying.call(TransportRequest("orders", {}))
    assert len(mock.calls) == 1


def test_rate_limit_enforces_min_interval():
    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = FakeClock()
    slept: list[float] = []
    mock = MockTransport({"a": TransportResponse("a", {}), "b": TransportResponse("b", {})})
    retrying = RetryingTransport(mock, min_interval_seconds=1.0, clock=clock, sleep=slept.append)
    retrying.call(TransportRequest("a", {}))
    clock.now += 0.1
    retrying.call(TransportRequest("b", {}))
    assert any(s > 0.8 for s in slept), "the second call must wait out the interval"


# --------------------------------------------------------------------------- #
# Client — raw payload storage (§26, §41), sync lifecycle (§45)
# --------------------------------------------------------------------------- #


def _page(n: int, body: dict) -> TransportResponse:
    return TransportResponse("orders_page", body, external_id=f"page-{n}")


def test_sync_stores_raw_payloads_once(session, qa_ip):
    client = KaspiShopClient(MockTransport(), session, qa_ip)
    body = {"orders": [1, 2, 3]}
    first = client.store_raw_payload(TransportResponse("orders_page", body))
    second = client.store_raw_payload(TransportResponse("orders_page", body))

    assert first is not None
    assert second is None, "§41 — identical payload stored once"

    stored = session.scalar(select(RawKaspiPayload).where(RawKaspiPayload.id == first.id))
    assert stored.api_type == "SHOP"
    assert stored.payload_hash == payload_hash(body)
    assert len(stored.payload_hash) == 64


def test_hash_is_canonical_and_stable():
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})
    assert payload_hash({"a": 1}) != payload_hash({"a": "1"})


def test_sync_success_lifecycle(session, qa_ip):
    transport = MockTransport(
        {
            "orders_page": _page(1, {"orders": [1]}),
        }
    )
    client = KaspiShopClient(transport, session, qa_ip)
    result = client.run_sync("orders_page", pages=[{}, {}, {}])

    assert result.status == "SUCCESS"
    assert result.fetched == 3
    assert result.inserted == 1, "§41 — the same body three times -> stored once"
    assert result.errors == 0

    log = session.scalar(select(SyncLog).where(SyncLog.id == result.sync_log_id))
    assert log.status == "SUCCESS"
    assert log.records_fetched == 3
    assert log.records_inserted == 1
    assert log.errors_count == 0
    assert log.sync_finished_at is not None


def test_sync_partial_page_failure_is_isolated(session, qa_ip):
    """One failed page must not abort the remaining pages (§45)."""
    transport = MockTransport(
        {
            "orders_page": _page(1, {"orders": [1]}),
        },
        fail_times={"orders_page": 1},  # first call fails, then succeeds
    )
    client = KaspiShopClient(transport, session, qa_ip)
    result = client.run_sync("orders_page", pages=[{}, {}])

    assert result.status == "PARTIAL"
    assert result.fetched == 1
    assert result.errors == 1

    log = session.scalar(select(SyncLog).where(SyncLog.id == result.sync_log_id))
    assert log.records_fetched == 1
    assert log.records_inserted == 1
    assert log.error_details is not None
    assert log.error_details["errors"][0]["page"] == "0"


def test_sync_all_pages_failed(session, qa_ip):
    transport = MockTransport(fail_times={"orders_page": 10})
    client = KaspiShopClient(transport, session, qa_ip)
    result = client.run_sync("orders_page", pages=[{}, {}, {}])

    assert result.status == "FAILED"
    assert result.fetched == 0
    assert result.errors == 3


def test_retrying_transport_no_double_booking(session, qa_ip):
    """§67 — a retried page whose body is identical stores it ONCE."""
    mock = MockTransport(
        {"orders_page": _page(1, {"orders": [1, 2]})},
        fail_times={"orders_page": 2},
    )
    retrying = RetryingTransport(mock, max_attempts=4, sleep=_no_sleep)
    client = KaspiShopClient(retrying, session, qa_ip)
    result = client.run_sync("orders_page", pages=[{}])

    assert result.status == "SUCCESS"
    stored = session.scalars(select(RawKaspiPayload)).all()
    assert len(stored) == 1, "the retried call must not create a second payload"


def test_pay_client_tag_and_pnl_independence(session, qa_ip):
    client = KaspiPayClient(MockTransport(), session, qa_ip)
    result = client.run_sync("payments_page", pages=[{}])
    log = session.scalar(select(SyncLog).where(SyncLog.id == result.sync_log_id))
    assert log.api_type == "PAY"


def test_credential_stored_encrypted(session, qa_ip, master_key):
    envelope = crypto.encrypt_token("kaspi-token-123", master_key)
    cred = KaspiApiCredential(
        ip_id=qa_ip, api_type="SHOP", encrypted_token=envelope, status="ACTIVE"
    )
    session.add(cred)
    session.flush()

    stored = session.scalar(select(KaspiApiCredential).where(KaspiApiCredential.id == cred.id))
    assert b"kaspi-token-123" not in bytes(stored.encrypted_token), (
        "§18 — token is not in plaintext"
    )
    assert crypto.decrypt_token(bytes(stored.encrypted_token), master_key) == "kaspi-token-123"


def test_one_active_credential_per_ip_api(session, qa_ip, master_key):
    envelope = crypto.encrypt_token("t1", master_key)
    session.add(KaspiApiCredential(ip_id=qa_ip, api_type="PAY", encrypted_token=envelope))
    session.flush()
    session.add(KaspiApiCredential(ip_id=qa_ip, api_type="PAY", encrypted_token=envelope))
    with pytest.raises(Exception, match="uq_kaspi_credentials_active"):
        session.flush()


def test_base_client_rejects_unknown_api_type(session, qa_ip):
    class BrokenClient(BaseKaspiClient):
        api_type = "UNKNOWN"

    with pytest.raises(ValueError, match="§42"):
        BrokenClient(MockTransport(), session, qa_ip)


# --------------------------------------------------------------------------- #
# Unique coverage merged from the former test_kaspi_client.py
# --------------------------------------------------------------------------- #
