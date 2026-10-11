"""
Bank statement pipeline models (G-BANK-2..5).

1:1 mapping of database/schema.sql SECTION 3b:
  raw_bank_statements   [ARCHITECTURAL EXTENSION, G-BANK-2]
  bank_statement_lines  [ARCHITECTURAL EXTENSION, G-BANK-3]
  bank_fee_knp_codes    [ARCHITECTURAL EXTENSION, G-BANK-4]

Behaviour that lives in the database, not here:
  * bank_statement_lines rows are external facts — immutable after import.
    BEFORE UPDATE/DELETE trigger fn_bank_statement_lines_immutable() blocks
    every fact change; only the reconciliation workflow columns
    (match_status / matched_operation_id / matched_at / matched_by) may
    change. DELETE is gated on the strong pattern (GUC
    app.allow_financial_delete + erp_migrator membership, TD-003).
  * uq_bank_statement_lines_fingerprint (ip_id, line_fingerprint) makes
    overlapping re-imports idempotent (F-ETL-008 pattern); the service
    catches IntegrityError per-row inside a savepoint.
  * uq_raw_bank_statements_file (ip_id, provider_code, file_hash) rejects a
    second import of the same file; concurrent imports race on it and the
    loser is treated as a duplicate file.
  * bank_fee_knp_codes is created EMPTY on purpose (§94): commission KNP
    codes are facts from real statements, never invented.
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
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import (
    TIMESTAMPTZ,
    Base,
    created_at_column,
    money_column,
    pk_column,
)

if TYPE_CHECKING:
    pass


class RawBankStatement(Base):
    """ARCHITECTURAL EXTENSION — source bank statement file, verbatim (G-BANK-2)."""

    __tablename__ = "raw_bank_statements"
    __table_args__ = (
        CheckConstraint(
            "provider_code IN ('HALYK','FREEDOM','EPAY','KASPI_PAY','MANUAL')",
            name="chk_raw_bank_statements_provider",
        ),
        CheckConstraint(
            "file_hash ~ '^[0-9a-f]{64}$'",
            name="chk_raw_bank_statements_file_hash",
        ),
        CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="chk_raw_bank_statements_currency",
        ),
        CheckConstraint(
            "period_to IS NULL OR period_from IS NULL OR period_to >= period_from",
            name="chk_raw_bank_statements_period",
        ),
        UniqueConstraint(
            "ip_id",
            "provider_code",
            "file_hash",
            name="uq_raw_bank_statements_file",
        ),
        Index("ix_raw_bank_statements_ip", "ip_id"),
        Index("ix_raw_bank_statements_batch", "import_batch_id"),
        Index("ix_raw_bank_statements_account", "account_id"),
        {
            "comment": "ARCHITECTURAL EXTENSION — source bank statement file "
            "verbatim (G-BANK-2). Provenance for all statement lines."
        },
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ip_entities.id", ondelete="RESTRICT"), nullable=False
    )
    provider_code: Mapped[str] = mapped_column(String(20), nullable=False)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True
    )
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    statement_no: Mapped[str | None] = mapped_column(String(50), nullable=True)
    period_from: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    period_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    opening_balance: Mapped[decimal.Decimal | None] = money_column(nullable=True)
    closing_balance: Mapped[decimal.Decimal | None] = money_column(nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    import_batch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("import_batches.id", ondelete="RESTRICT"), nullable=True
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    processed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class BankStatementLine(Base):
    """ARCHITECTURAL EXTENSION — normalized statement line, external fact (G-BANK-3)."""

    __tablename__ = "bank_statement_lines"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('IN','OUT')",
            name="chk_bank_statement_lines_direction",
        ),
        CheckConstraint("amount > 0", name="chk_bank_statement_lines_amount_positive"),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="chk_bank_statement_lines_currency",
        ),
        CheckConstraint(
            "counterparty_iin IS NULL OR counterparty_iin ~ '^[0-9]{12}$'",
            name="chk_bank_statement_lines_iin",
        ),
        CheckConstraint(
            "line_fingerprint ~ '^[0-9a-f]{64}$'",
            name="chk_bank_statement_lines_fingerprint",
        ),
        CheckConstraint(
            "provider_code IN ('HALYK','FREEDOM','EPAY','KASPI_PAY','MANUAL')",
            name="chk_bank_statement_lines_provider",
        ),
        CheckConstraint(
            "match_status IN ('UNMATCHED','SUGGESTED','CONFIRMED','IGNORED')",
            name="chk_bank_statement_lines_match_status",
        ),
        CheckConstraint(
            "(match_status IN ('SUGGESTED','CONFIRMED') AND matched_operation_id IS NOT NULL)"
            " OR (match_status IN ('UNMATCHED','IGNORED') AND matched_operation_id IS NULL)",
            name="chk_bank_statement_lines_match_consistency",
        ),
        CheckConstraint("line_no >= 0", name="chk_bank_statement_lines_line_no"),
        UniqueConstraint(
            "ip_id",
            "line_fingerprint",
            name="uq_bank_statement_lines_fingerprint",
        ),
        Index("ix_bank_statement_lines_statement", "statement_id"),
        Index("ix_bank_statement_lines_match", "ip_id", "match_status"),
        Index("ix_bank_statement_lines_value_date", "ip_id", "value_date"),
        Index("ix_bank_statement_lines_account", "account_id"),
        # Framework §1 hard gate: one operation is CONFIRMED-matched at most
        # once. SUGGESTED rows are excluded (human picks one candidate).
        Index(
            "uq_bank_statement_lines_confirmed_op",
            "matched_operation_id",
            postgresql_where=text("match_status = 'CONFIRMED'"),
        ),
        {
            "comment": "ARCHITECTURAL EXTENSION — normalized bank statement "
            "lines (G-BANK-3). Immutable external facts; the ledger link is "
            "only via matched_operation_id."
        },
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ip_entities.id", ondelete="RESTRICT"), nullable=False
    )
    statement_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("raw_bank_statements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True
    )

    provider_code: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_account: Mapped[str] = mapped_column(String(64), nullable=False)
    value_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    entry_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[decimal.Decimal] = money_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'KZT'"))
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    document_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bank_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)

    counterparty_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    counterparty_iin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    counterparty_bik: Mapped[str | None] = mapped_column(String(20), nullable=True)
    counterparty_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    knp_code: Mapped[str | None] = mapped_column(String(10), nullable=True)

    balance_after: Mapped[decimal.Decimal | None] = money_column(nullable=True)
    raw_line: Mapped[str] = mapped_column(Text, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    line_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    match_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'UNMATCHED'")
    )
    matched_operation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("operations.id", ondelete="RESTRICT"), nullable=True
    )
    matched_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    matched_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime.datetime] = created_at_column()


class BankFeeKnpCode(Base):
    """ARCHITECTURAL EXTENSION — KNP codes identifying bank commissions (G-BANK-4).

    Created EMPTY on purpose (§94): commission KNP codes are facts from real
    statements, never invented.
    """

    __tablename__ = "bank_fee_knp_codes"
    __table_args__ = (
        CheckConstraint(
            "knp_code ~ '^[0-9]{1,10}$'",
            name="chk_bank_fee_knp_codes_code",
        ),
        {
            "comment": "ARCHITECTURAL EXTENSION — KNP codes of bank commissions "
            "(G-BANK-4). Filled only from real statements."
        },
    )

    knp_code: Mapped[str] = mapped_column(String(10), primary_key=True, autoincrement=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
