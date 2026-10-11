"""
Financial ledger models.

1:1 mapping of database/schema.sql:
  operations     [ORIGINAL #8, §27, §16, §41]
  transfers      [ORIGINAL #9, §28, §76]
  import_batches [ARCHITECTURAL EXTENSION, §39–§41]

Behaviour that lives in the database, not here:
  * §16 — physical DELETE on operations / transfers is blocked by
    fn_forbid_delete(); corrections are made with a reversal operation;
  * §27 — the sign of amount is validated against operation_types.direction
    by fn_check_operation_sign();
  * §21 — category kind must match operation kind
    (fn_check_operation_category());
  * §41 — partial unique indexes make ETL and API retries idempotent
    (ADR-010); the application must rely on them instead of a
    "SELECT then INSERT" check, which is not race-safe (§57).
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
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import (
    TIMESTAMPTZ,
    Base,
    bool_column,
    counter_column,
    created_at_column,
    money_column,
    pk_column,
    updated_at_column,
)

if TYPE_CHECKING:
    from app.models.kaspi import RawKaspiPayload
    from app.models.orders import Order
    from app.models.reference import (
        Account,
        Category,
        Counterparty,
        IpEntity,
        OperationType,
    )


class ImportBatch(Base):
    """
    ARCHITECTURAL EXTENSION — ETL / sync run registry with dry-run
    reporting (§39–§41).
    """

    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('EXCEL', 'KASPI_SHOP', 'KASPI_PAY', 'BANK', 'MANUAL')",
            name="chk_import_batches_source_type",
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'DRY_RUN')",
            name="chk_import_batches_status",
        ),
        CheckConstraint(
            "rows_total >= 0 AND rows_inserted >= 0 AND rows_skipped >= 0 "
            "AND rows_duplicates >= 0 AND rows_errors >= 0",
            name="chk_import_batches_counters",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="chk_import_batches_finished",
        ),
        CheckConstraint(
            "(is_dry_run = TRUE  AND status IN ('RUNNING', 'DRY_RUN', 'FAILED')) OR "
            "(is_dry_run = FALSE)",
            name="chk_import_batches_dry_run",
        ),
        Index("idx_import_batches_started", "started_at"),
        Index(
            "idx_import_batches_source_hash",
            "source_hash",
            postgresql_where=text("source_hash IS NOT NULL"),
        ),
        {
            "comment": "ARCHITECTURAL EXTENSION — ETL / sync run registry with "
            "dry-run reporting (MASTER PROMPT §39–§41)."
        },
    )

    id: Mapped[int] = pk_column()
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str | None] = mapped_column(
        CHAR(64),
        nullable=True,
        comment="SHA-256 of the source file — detects re-import of an identical file (§41).",
    )
    ip_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=True,
    )
    is_dry_run: Mapped[bool] = bool_column(default=False)
    started_at: Mapped[datetime.datetime] = created_at_column()
    finished_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    rows_total: Mapped[int] = counter_column()
    rows_inserted: Mapped[int] = counter_column()
    rows_skipped: Mapped[int] = counter_column()
    rows_duplicates: Mapped[int] = counter_column()
    rows_errors: Mapped[int] = counter_column()
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'RUNNING'"),
    )
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    ip_entity: Mapped[IpEntity | None] = relationship()


class Operation(Base):
    """
    Central financial ledger entry (§27).

    DELETE is forbidden — use reversal / correction (§16).
    """

    __tablename__ = "operations"
    __table_args__ = (
        # §27 — amount must never be zero.
        CheckConstraint("amount <> 0", name="chk_operations_amount_nonzero"),
        CheckConstraint(
            "status IN ('POSTED', 'REVERSED', 'CORRECTED', 'DRAFT')",
            name="chk_operations_status",
        ),
        CheckConstraint(
            "related_operation_id IS NULL OR related_operation_id <> id",
            name="chk_operations_no_self_reference",
        ),
        # ADR-016 Q3/Q5: compensating-reversal link + mandatory reason.
        CheckConstraint(
            "(reverses_operation_id IS NULL) = (reversal_reason IS NULL)",
            name="chk_operations_reversal_link",
        ),
        CheckConstraint(
            "reverses_operation_id IS NULL OR reverses_operation_id <> id",
            name="chk_operations_reversal_no_self",
        ),
        # §64 — mandatory index name.
        Index("idx_operations_analytics", "ip_id", "operation_date", "type_id"),
        Index("idx_operations_date", "operation_date"),
        Index("idx_operations_category", "category_id"),
        Index("idx_operations_counterparty", "counterparty_id"),
        Index("idx_operations_related_order", "related_order_id"),
        Index("idx_operations_related_op", "related_operation_id"),
        # ADR-016 Q3: index for reversal lookups.
        Index(
            "ix_operations_reverses_op",
            "reverses_operation_id",
            postgresql_where=text("reverses_operation_id IS NOT NULL"),
        ),
        Index("idx_operations_raw_payload", "raw_payload_id"),
        Index("idx_operations_account", "account_id"),
        Index("idx_operations_import_batch", "import_batch_id"),
        Index(
            "idx_operations_status",
            "status",
            postgresql_where=text("status <> 'POSTED'"),
        ),
        # §41 / ADR-010 — race-safe idempotency for API and ETL retries.
        Index(
            "uq_operations_ip_type_external",
            "ip_id",
            "type_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index(
            "uq_operations_import_fingerprint",
            "ip_id",
            "import_fingerprint",
            unique=True,
            postgresql_where=text("import_fingerprint IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["account_id", "ip_id"],
            ["accounts.id", "accounts.ip_id"],
            ondelete="RESTRICT",
            name="fk_operations_account_ip",
        ),
        {
            "comment": "Central financial ledger. DELETE is forbidden — use "
            "reversal/correction (MASTER PROMPT §27, §16)."
        },
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    type_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("operation_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    category_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
    )
    counterparty_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("counterparties.id", ondelete="RESTRICT"),
        nullable=True,
    )
    amount: Mapped[decimal.Decimal] = money_column(
        nullable=False,
        comment="KZT, NUMERIC(15,2), never zero. Sign is validated against "
        "operation_types.direction (§27).",
    )
    operation_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    accrual_date: Mapped[datetime.date | None] = mapped_column(
        Date,
        nullable=True,
        comment="P&L recognition date (accrual basis). NULL = pure cash movement, "
        "excluded from P&L. P&L aggregates by COALESCE(accrual_date, operation_date); "
        "Cash Flow aggregates by operation_date. (BOOK_SYNTHESIS_2026-10-03)",
    )
    external_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Id in the source system. UNIQUE per (ip_id,type_id) when present — "
        "makes retries idempotent (§41, ADR-010).",
    )
    related_order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    related_operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="RESTRICT"),
        nullable=True,
        comment="Link to the reversed/corrected operation, and pairs TRANSFER_OUT "
        "with TRANSFER_IN (§16, §28).",
    )
    # ADR-016 Q3/Q5: compensating-reversal link. The original operation is
    # never mutated; a reversal is a new POSTED operation pointing back.
    reverses_operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="RESTRICT"),
        nullable=True,
        comment="ADR-016: the original operation this compensating reversal offsets.",
    )
    reversal_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="ADR-016 Q5: mandatory business reason for the reversal.",
    )
    raw_payload_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("raw_kaspi_payloads.id", ondelete="SET NULL"),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    # production additions (§81)
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'POSTED'"),
    )
    import_batch_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("import_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    import_fingerprint: Mapped[str | None] = mapped_column(
        CHAR(64),
        nullable=True,
        comment="SHA-256 of the normalised source row — prevents duplicate Excel import (§41).",
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    ip_entity: Mapped[IpEntity] = relationship()
    operation_type: Mapped[OperationType] = relationship()
    category: Mapped[Category | None] = relationship()
    counterparty: Mapped[Counterparty | None] = relationship()
    account: Mapped[Account | None] = relationship(
        back_populates="operations",
        foreign_keys=[account_id],
    )
    related_order: Mapped[Order | None] = relationship()
    related_operation: Mapped[Operation | None] = relationship(
        remote_side="Operation.id",
        foreign_keys=[related_operation_id],
    )
    raw_payload: Mapped[RawKaspiPayload | None] = relationship()
    import_batch: Mapped[ImportBatch | None] = relationship()


class Transfer(Base):
    """
    Cross-IP movement between explicitly selected accounts. Its two ledger legs
    are excluded from P&L by the transfer operation types (§28, §76).
    """

    __tablename__ = "transfers"
    __table_args__ = (
        # §28 — from_ip_id <> to_ip_id.
        CheckConstraint("from_ip_id <> to_ip_id", name="chk_transfers_different_ip"),
        CheckConstraint("amount > 0", name="chk_transfers_amount"),
        CheckConstraint(
            "status IN ('POSTED', 'REVERSED', 'DRAFT')",
            name="chk_transfers_status",
        ),
        CheckConstraint(
            "status <> 'POSTED' OR (outbound_operation_id IS NOT NULL "
            "AND inbound_operation_id IS NOT NULL)",
            name="chk_transfers_posted_has_legs",
        ),
        CheckConstraint(
            "from_account_id IS NULL OR to_account_id IS NULL "
            "OR from_account_id <> to_account_id",
            name="chk_transfers_accounts_distinct",
        ),
        CheckConstraint(
            "idempotency_key IS NULL OR (length(btrim(idempotency_key)) > 0 "
            "AND from_account_id IS NOT NULL AND to_account_id IS NOT NULL "
            "AND created_by IS NOT NULL AND outbound_operation_id IS NOT NULL "
            "AND inbound_operation_id IS NOT NULL)",
            name="chk_transfers_keyed_pair",
        ),
        CheckConstraint(
            "outbound_operation_id IS NULL OR inbound_operation_id IS NULL "
            "OR outbound_operation_id <> inbound_operation_id",
            name="chk_transfers_operations_distinct",
        ),
        ForeignKeyConstraint(
            ["from_account_id", "from_ip_id"],
            ["accounts.id", "accounts.ip_id"],
            ondelete="RESTRICT",
            name="fk_transfers_from_account_ip",
        ),
        ForeignKeyConstraint(
            ["to_account_id", "to_ip_id"],
            ["accounts.id", "accounts.ip_id"],
            ondelete="RESTRICT",
            name="fk_transfers_to_account_ip",
        ),
        UniqueConstraint("outbound_operation_id", name="uq_transfers_outbound"),
        UniqueConstraint("inbound_operation_id", name="uq_transfers_inbound"),
        Index("uq_transfers_idempotency_key", "idempotency_key", unique=True),
        Index("idx_transfers_from", "from_ip_id", "transfer_date"),
        Index("idx_transfers_to", "to_ip_id", "transfer_date"),
        # P2-2 FK indexes (declared to match schema.sql).
        Index(
            "idx_transfers_from_account",
            "from_account_id",
            postgresql_where=text("from_account_id IS NOT NULL"),
        ),
        Index(
            "idx_transfers_to_account",
            "to_account_id",
            postgresql_where=text("to_account_id IS NOT NULL"),
        ),
        {
            "comment": "Transfers between ИП: TRANSFER_OUT + TRANSFER_IN. Never "
            "income or expense (MASTER PROMPT §28, §76)."
        },
    )

    id: Mapped[int] = pk_column()
    from_ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    to_ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    to_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    amount: Mapped[decimal.Decimal] = money_column(
        nullable=False,
        comment="Always positive; the direction is expressed by the two linked "
        "operations (§28).",
    )
    transfer_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outbound_operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    inbound_operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # production additions (§81)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'POSTED'"),
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    from_ip: Mapped[IpEntity] = relationship(foreign_keys=[from_ip_id])
    to_ip: Mapped[IpEntity] = relationship(foreign_keys=[to_ip_id])
    outbound_operation: Mapped[Operation | None] = relationship(
        foreign_keys=[outbound_operation_id],
    )
    inbound_operation: Mapped[Operation | None] = relationship(
        foreign_keys=[inbound_operation_id],
    )


class SettlementLink(Base):
    """
    Many-to-many accrual↔payment matching (ACCRUAL_MODEL_DESIGN_2026-10-03).

    Stores relationship assertions, not money. Paid/remaining amounts are
    always DERIVED via SUM(allocated_amount), never stored — no false
    "fully paid" state can exist.
    """

    __tablename__ = "settlement_links"
    __table_args__ = (
        CheckConstraint("allocated_amount > 0", name="chk_settlement_links_amount_positive"),
        CheckConstraint(
            "accrual_operation_id <> payment_operation_id",
            name="chk_settlement_links_no_self_link",
        ),
        Index("ix_settlement_links_accrual", "accrual_operation_id"),
        Index("ix_settlement_links_payment", "payment_operation_id"),
        Index("ix_settlement_links_ip", "ip_id"),
        {
            "comment": "Many-to-many accrual↔payment matching. Stores relationship "
            "assertions, not money. (ACCRUAL_MODEL_DESIGN_2026-10-03)"
        },
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    accrual_operation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_operation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("operations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    allocated_amount: Mapped[decimal.Decimal] = money_column(
        nullable=False,
        comment="Portion of payment allocated to this accrual. Must be > 0.",
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    created_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    accrual_operation: Mapped[Operation] = relationship(
        foreign_keys=[accrual_operation_id],
    )
    payment_operation: Mapped[Operation] = relationship(
        foreign_keys=[payment_operation_id],
    )


__all__ = ["ImportBatch", "Operation", "SettlementLink", "Transfer"]
