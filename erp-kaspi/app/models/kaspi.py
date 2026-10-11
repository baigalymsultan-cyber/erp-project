"""
Kaspi integration models.

1:1 mapping of database/schema.sql:
  kaspi_api_credentials [ORIGINAL #2,  §18, §19]
  raw_kaspi_payloads    [ORIGINAL #7,  §26]
  sync_log              [ORIGINAL #10, §45]
  kaspi_payouts         [ARCHITECTURAL EXTENSION, §24, §44]
  kaspi_payout_items    [ARCHITECTURAL EXTENSION, §44]

Security notes (§18, §19, ADR-009):
  * encrypted_token holds an AES-256-GCM envelope
    key_version(1) || nonce(12) || ciphertext || tag(16);
  * the encryption key lives in the environment and is NEVER stored in the
    database — only key_version is persisted;
  * the plaintext token must never be logged, returned by an API or written
    to audit_log (fn_audit() masks the column).

BLOCKED — EXTERNAL API SPECIFICATION REQUIRED (§94, OQ-003): the shape of the
Kaspi payloads is not invented here. raw payloads are stored verbatim in JSONB
and remain the source of truth until the official documentation is supplied.
"""

from __future__ import annotations

import datetime
import decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import (
    TIMESTAMPTZ,
    Base,
    counter_column,
    created_at_column,
    money_column,
    pk_column,
    updated_at_column,
)

if TYPE_CHECKING:
    from app.models.finance import Operation
    from app.models.orders import Order
    from app.models.reference import IpEntity


class KaspiApiCredential(Base):
    """Kaspi API token per ИП, encrypted at rest (§18, §19)."""

    __tablename__ = "kaspi_api_credentials"
    __table_args__ = (
        CheckConstraint(
            "api_type IN ('SHOP', 'PAY')",
            name="chk_kaspi_api_credentials_api_type",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'EXPIRED', 'ERROR', 'DISABLED')",
            name="chk_kaspi_api_credentials_status",
        ),
        # AES-256-GCM envelope: key_version(1) || nonce(12) || ciphertext(>=1) || tag(16)
        CheckConstraint(
            "octet_length(encrypted_token) >= 30",
            name="chk_kaspi_api_credentials_token_len",
        ),
        CheckConstraint("key_version >= 1", name="chk_kaspi_api_credentials_key_ver"),
        CheckConstraint(
            "status <> 'EXPIRED' OR expires_at IS NOT NULL",
            name="chk_kaspi_api_credentials_expired",
        ),
        Index("idx_kaspi_credentials_ip", "ip_id", "api_type", "status"),
        # Exactly one ACTIVE credential per (ИП, api_type) — race-safe.
        Index(
            "uq_kaspi_credentials_active",
            "ip_id",
            "api_type",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        {"comment": "Kaspi API tokens per ИП, encrypted at rest (MASTER PROMPT §18, §19)."},
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    api_type: Mapped[str] = mapped_column(String(10), nullable=False)
    encrypted_token: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment="AES-256-GCM envelope: key_version(1)||nonce(12)||ciphertext||tag(16). "
        "Plaintext is never stored or logged (§18, §19, ADR-009).",
    )
    expires_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()
    # production additions (§81 — original columns preserved)
    key_version: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("1"),
        comment="Encryption key version for rotation. The key itself is NEVER "
        "stored in the database (§19).",
    )
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    rotated_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)

    ip_entity: Mapped[IpEntity] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        # The token is deliberately never included in the representation (§18).
        return (
            f"<KaspiApiCredential id={self.id} ip_id={self.ip_id} "
            f"api_type={self.api_type!r} status={self.status!r}>"
        )


class RawKaspiPayload(Base):
    """Original Kaspi API payload kept for audit, debugging, reprocessing (§26)."""

    __tablename__ = "raw_kaspi_payloads"
    __table_args__ = (
        CheckConstraint(
            "api_type IS NULL OR api_type IN ('SHOP', 'PAY')",
            name="chk_raw_kaspi_payloads_api_type",
        ),
        CheckConstraint(
            "length(btrim(request_type)) > 0",
            name="chk_raw_kaspi_payloads_type",
        ),
        Index("idx_raw_payloads_ip_received", "ip_id", "received_at"),
        Index(
            "idx_raw_payloads_external",
            "external_id",
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index(
            "idx_raw_payloads_unprocessed",
            "received_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
        Index("idx_raw_payloads_payload_gin", "payload", postgresql_using="gin"),
        # §41 — identical responses are stored once.
        Index(
            "uq_raw_payloads_hash",
            "ip_id",
            "request_type",
            "payload_hash",
            unique=True,
            postgresql_where=text("payload_hash IS NOT NULL"),
        ),
        {
            "comment": "Original Kaspi API payloads kept for audit, debugging and "
            "reprocessing (MASTER PROMPT §26)."
        },
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    request_type: Mapped[str] = mapped_column(String(50), nullable=False)
    received_at: Mapped[datetime.datetime] = created_at_column()
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # production additions (§81)
    api_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(
        CHAR(64),
        nullable=True,
        comment="SHA-256 of the canonical payload — deduplicates identical responses (§41).",
    )
    processed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # FK fk_raw_kaspi_payloads_sync_log is created in SECTION 5 of schema.sql
    # (circular dependency raw_kaspi_payloads <-> sync_log).
    sync_log_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "sync_log.id",
            ondelete="SET NULL",
            name="fk_raw_kaspi_payloads_sync_log",
            use_alter=True,
        ),
        nullable=True,
    )

    ip_entity: Mapped[IpEntity] = relationship()
    sync_log: Mapped[SyncLog | None] = relationship(
        back_populates="payloads",
        foreign_keys=[sync_log_id],
    )
    orders: Mapped[list[Order]] = relationship(
        back_populates="raw_payload",
        foreign_keys="Order.raw_payload_id",
        passive_deletes="all",
    )


class SyncLog(Base):
    """Kaspi synchronisation run with counters and errors (§45)."""

    __tablename__ = "sync_log"
    __table_args__ = (
        CheckConstraint("api_type IN ('SHOP', 'PAY')", name="chk_sync_log_api_type"),
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED')",
            name="chk_sync_log_status",
        ),
        CheckConstraint(
            "records_fetched >= 0 AND records_inserted >= 0 "
            "AND records_updated >= 0 AND errors_count >= 0",
            name="chk_sync_log_counters",
        ),
        CheckConstraint(
            "sync_finished_at IS NULL OR sync_finished_at >= sync_started_at",
            name="chk_sync_log_finished",
        ),
        CheckConstraint(
            "period_to IS NULL OR period_from IS NULL OR period_to >= period_from",
            name="chk_sync_log_period",
        ),
        Index("idx_sync_log_ip_started", "ip_id", "api_type", "sync_started_at"),
        {"comment": "Kaspi synchronisation runs with counters and errors (MASTER PROMPT §45)."},
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    api_type: Mapped[str] = mapped_column(String(10), nullable=False)
    sync_started_at: Mapped[datetime.datetime] = created_at_column()
    sync_finished_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMPTZ,
        nullable=True,
    )
    records_fetched: Mapped[int] = counter_column()
    records_inserted: Mapped[int] = counter_column()
    records_updated: Mapped[int] = counter_column()
    errors_count: Mapped[int] = counter_column()
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # production additions (§81)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'RUNNING'"),
    )
    period_from: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    period_to: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)

    ip_entity: Mapped[IpEntity] = relationship()
    payloads: Mapped[list[RawKaspiPayload]] = relationship(
        back_populates="sync_log",
        foreign_keys="RawKaspiPayload.sync_log_id",
        passive_deletes="all",
    )


class KaspiPayout(Base):
    """
    ARCHITECTURAL EXTENSION — Kaspi payout registry (§24, §44).

    This is the step where Kaspi Shop revenue becomes real money.
    Amounts are stored exactly as reported by Kaspi; no commission or payout
    rule is inferred (OQ-004, OQ-005, §94).
    """

    __tablename__ = "kaspi_payouts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('EXPECTED', 'RECEIVED', 'RECONCILED', 'DISCREPANCY', 'CANCELLED')",
            name="chk_kaspi_payouts_status",
        ),
        CheckConstraint("gross_amount >= 0", name="chk_kaspi_payouts_gross"),
        CheckConstraint("commission_amount >= 0", name="chk_kaspi_payouts_commission"),
        CheckConstraint("delivery_amount >= 0", name="chk_kaspi_payouts_delivery"),
        CheckConstraint("refund_amount >= 0", name="chk_kaspi_payouts_refund"),
        UniqueConstraint("ip_id", "external_payout_id", name="uq_kaspi_payouts_external"),
        Index("idx_kaspi_payouts_ip_date", "ip_id", "payout_date"),
        Index(
            "idx_kaspi_payouts_status",
            "status",
            postgresql_where=text("status <> 'RECONCILED'"),
        ),
        # P2-2 FK indexes (declared to match schema.sql).
        Index(
            "idx_kaspi_payouts_operation",
            "operation_id",
            postgresql_where=text("operation_id IS NOT NULL"),
        ),
        Index(
            "idx_kaspi_payouts_raw_payload",
            "raw_payload_id",
            postgresql_where=text("raw_payload_id IS NOT NULL"),
        ),
        {
            "comment": "ARCHITECTURAL EXTENSION — Kaspi payout registry; the point "
            "where Shop revenue becomes cash (MASTER PROMPT §24, §44)."
        },
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_payout_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payout_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    gross_amount: Mapped[decimal.Decimal] = money_column()
    commission_amount: Mapped[decimal.Decimal] = money_column(default_zero=True)
    delivery_amount: Mapped[decimal.Decimal] = money_column(default_zero=True)
    refund_amount: Mapped[decimal.Decimal] = money_column(default_zero=True)
    net_amount: Mapped[decimal.Decimal] = money_column(
        nullable=False,
        comment="Net amount as reported by Kaspi. Stored, not computed — the payout "
        "rule is unknown (OQ-004, §94).",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'EXPECTED'"),
    )
    # FK fk_kaspi_payouts_operation is created in SECTION 5 of schema.sql
    # (circular dependency kaspi_payouts <-> operations).
    operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "operations.id",
            ondelete="RESTRICT",
            name="fk_kaspi_payouts_operation",
            use_alter=True,
        ),
        nullable=True,
    )
    raw_payload_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("raw_kaspi_payloads.id", ondelete="SET NULL"),
        nullable=True,
    )
    reconciled_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    ip_entity: Mapped[IpEntity] = relationship()
    operation: Mapped[Operation | None] = relationship(foreign_keys=[operation_id])
    raw_payload: Mapped[RawKaspiPayload | None] = relationship(
        foreign_keys=[raw_payload_id],
    )
    items: Mapped[list[KaspiPayoutItem]] = relationship(
        back_populates="payout",
        cascade="all, delete-orphan",
    )


class KaspiPayoutItem(Base):
    """
    ARCHITECTURAL EXTENSION — payout line used for
    Order ↔ Payout ↔ Operation reconciliation (§44).
    """

    __tablename__ = "kaspi_payout_items"
    __table_args__ = (
        CheckConstraint(
            "item_type IN ('SALE', 'REFUND', 'ADJUSTMENT', 'FEE', 'DELIVERY')",
            name="chk_kaspi_payout_items_type",
        ),
        CheckConstraint(
            "match_status IN ('UNMATCHED', 'MATCHED', 'AMOUNT_MISMATCH', "
            "'COMMISSION_MISMATCH', 'DELIVERY_MISMATCH', 'DUPLICATE', "
            "'MISSING_ORDER', 'PARTIAL', 'UNEXPLAINED')",
            name="chk_kaspi_payout_items_match",
        ),
        CheckConstraint(
            "commission_amount >= 0 AND delivery_amount >= 0 AND refund_amount >= 0",
            name="chk_kaspi_payout_items_amounts",
        ),
        CheckConstraint(
            "order_id IS NOT NULL OR external_order_id IS NOT NULL "
            "OR item_type IN ('ADJUSTMENT', 'FEE')",
            name="chk_kaspi_payout_items_order_ref",
        ),
        Index("idx_kaspi_payout_items_payout", "payout_id"),
        Index("idx_kaspi_payout_items_order", "order_id"),
        Index(
            "idx_kaspi_payout_items_ext_ord",
            "external_order_id",
            postgresql_where=text("external_order_id IS NOT NULL"),
        ),
        Index(
            "idx_kaspi_payout_items_match",
            "match_status",
            postgresql_where=text("match_status <> 'MATCHED'"),
        ),
        # One line per (payout, order, item_type) — blocks double counting.
        Index(
            "uq_kaspi_payout_items_order",
            "payout_id",
            "order_id",
            "item_type",
            unique=True,
            postgresql_where=text("order_id IS NOT NULL"),
        ),
        {
            "comment": "ARCHITECTURAL EXTENSION — payout lines used for "
            "Order ↔ Payout ↔ Operation reconciliation (MASTER PROMPT §44)."
        },
    )

    id: Mapped[int] = pk_column()
    payout_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("kaspi_payouts.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    external_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gross_amount: Mapped[decimal.Decimal] = money_column()
    commission_amount: Mapped[decimal.Decimal] = money_column(default_zero=True)
    delivery_amount: Mapped[decimal.Decimal] = money_column(default_zero=True)
    refund_amount: Mapped[decimal.Decimal] = money_column(default_zero=True)
    net_amount: Mapped[decimal.Decimal] = money_column()
    item_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'SALE'"),
    )
    match_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        server_default=text("'UNMATCHED'"),
        comment="Reconciliation verdict — covers all 8 discrepancy classes of §44.",
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    payout: Mapped[KaspiPayout] = relationship(back_populates="items")
    order: Mapped[Order | None] = relationship()


__all__ = [
    "KaspiApiCredential",
    "KaspiPayout",
    "KaspiPayoutItem",
    "RawKaspiPayload",
    "SyncLog",
]
