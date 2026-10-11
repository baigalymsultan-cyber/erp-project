"""
Employees, piecework and payroll models.

1:1 mapping of database/schema.sql:
  employees            [ORIGINAL #11, §29]
  work_operation_types [ORIGINAL #12, §30]
  rates_history        [ORIGINAL #13, §31]
  work_entries         [ORIGINAL #14, §32, §33]
  payroll_periods      [ORIGINAL #15, §34]
  payroll_summary      [ORIGINAL #16, §35]
  payroll_payments     [ORIGINAL #17, §36]

Behaviour that lives in the database, not here (§32, §33, §35, §16):
  * work_entries.rate_at_time is frozen on INSERT by fn_work_entry_freeze_rate()
    and made immutable by fn_work_entry_protect_rate();
  * work_entries.total_sum and payroll_summary.total_calculated are
    GENERATED ALWAYS ... STORED — the application must never assign them;
  * DELETE on work_entries / payroll_summary / payroll_payments is blocked by
    fn_forbid_delete().
"""

from __future__ import annotations

import datetime
import decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import (
    MONEY,
    TIMESTAMPTZ,
    Base,
    bool_column,
    created_at_column,
    money_column,
    pk_column,
    quantity_column,
    updated_at_column,
)

if TYPE_CHECKING:
    from app.models.finance import ImportBatch, Operation
    from app.models.orders import Order
    from app.models.reference import IpEntity


class Employee(Base):
    """Production or office employee (§29)."""

    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'ON_LEAVE', 'FIRED')", name="chk_employees_status"),
        CheckConstraint(
            "payment_type IN ('PIECEWORK', 'SALARY', 'MIXED')",
            name="chk_employees_payment_type",
        ),
        CheckConstraint("length(btrim(full_name)) > 0", name="chk_employees_full_name"),
        CheckConstraint(
            "fired_at IS NULL OR hired_at IS NULL OR fired_at >= hired_at",
            name="chk_employees_dates",
        ),
        CheckConstraint(
            "(status = 'FIRED'  AND fired_at IS NOT NULL) OR "
            "(status <> 'FIRED' AND fired_at IS NULL)",
            name="chk_employees_fired_status",
        ),
        CheckConstraint(
            "(payment_type = 'PIECEWORK' AND base_salary IS NULL) OR "
            "(payment_type IN ('SALARY', 'MIXED') AND base_salary IS NOT NULL "
            "AND base_salary >= 0)",
            name="chk_employees_base_salary",
        ),
        Index("idx_employees_ip_status", "assigned_ip_id", "status"),
        {"comment": "Production and office employees (MASTER PROMPT §29)."},
    )

    id: Mapped[int] = pk_column()
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    position: Mapped[str | None] = mapped_column(String(255), nullable=True)
    assigned_ip_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=True,
    )
    hired_at: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    fired_at: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    payment_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="PIECEWORK (сдельная) | SALARY (оклад) | MIXED (смешанная) (§29).",
    )
    # production additions (§81)
    base_salary: Mapped[decimal.Decimal | None] = mapped_column(
        MONEY,
        nullable=True,
        comment="Monthly base salary in KZT for SALARY/MIXED employees; NULL for PIECEWORK (§35).",
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    assigned_ip: Mapped[IpEntity | None] = relationship()
    work_entries: Mapped[list[WorkEntry]] = relationship(
        back_populates="employee",
        passive_deletes="all",
    )


class WorkOperationType(Base):
    """Piecework operation catalogue — 11 mandatory types (§30)."""

    __tablename__ = "work_operation_types"
    __table_args__ = (
        CheckConstraint(
            "calc_mode IN ('per_unit', 'per_hour', 'per_order')",
            name="chk_work_operation_types_calc_mode",
        ),
        CheckConstraint("length(btrim(unit)) > 0", name="chk_work_operation_types_unit"),
        CheckConstraint("length(btrim(name)) > 0", name="chk_work_operation_types_name"),
        {"comment": "Piecework operation catalogue — 11 mandatory types (MASTER PROMPT §30)."},
    )

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Unit of measure: м2, пог_м, отверстие, шт, часы, рейс (§30).",
    )
    calc_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="per_unit | per_hour | per_order (§30).",
    )
    is_active: Mapped[bool] = bool_column(default=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    rates: Mapped[list[RateHistory]] = relationship(
        back_populates="work_operation_type",
        passive_deletes="all",
    )


class RateHistory(Base):
    """
    Piecework rate history (§31).

    Periods of one work operation type never overlap — enforced by the
    EXCLUDE constraint, not by application code.
    """

    __tablename__ = "rates_history"
    __table_args__ = (
        CheckConstraint("rate >= 0", name="chk_rates_history_rate"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="chk_rates_history_period",
        ),
        ExcludeConstraint(
            ("work_operation_type_id", "="),
            (text("tstzrange(effective_from, effective_to, '[)')"), "&&"),
            name="excl_rates_history_overlap",
            using="gist",
        ),
        Index("idx_rates_lookup", "work_operation_type_id", "effective_from", "effective_to"),
        {
            "comment": "Piecework rate history; periods of one type never overlap "
            "(MASTER PROMPT §31)."
        },
    )

    id: Mapped[int] = pk_column()
    work_operation_type_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("work_operation_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    rate: Mapped[decimal.Decimal] = money_column()
    effective_from: Mapped[datetime.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    effective_to: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMPTZ,
        nullable=True,
        comment="NULL means the rate is currently in force. Half-open interval [from, to).",
    )
    # production additions (§81)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    work_operation_type: Mapped[WorkOperationType] = relationship(back_populates="rates")


class PayrollPeriod(Base):
    """Payroll period (§34). Periods never overlap (EXCLUDE constraint)."""

    __tablename__ = "payroll_periods"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="chk_payroll_periods_dates"),
        CheckConstraint(
            "status IN ('OPEN', 'PROCESSING', 'CLOSED', 'PAID')",
            name="chk_payroll_periods_status",
        ),
        UniqueConstraint("start_date", "end_date", name="uq_payroll_periods_range"),
        ExcludeConstraint(
            (text("daterange(start_date, end_date, '[]')"), "&&"),
            name="excl_payroll_periods_overlap",
            using="gist",
        ),
        {"comment": "Payroll periods (MASTER PROMPT §34)."},
    )

    id: Mapped[int] = pk_column()
    start_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    end_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'OPEN'"),
        comment="OPEN | PROCESSING | CLOSED | PAID (§34).",
    )
    # production additions (§81)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()
    closed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


class WorkEntry(Base):
    """
    Piecework entry — the labour part of direct cost (§33, §37).

    rate_at_time is frozen by trigger on INSERT and immutable afterwards (§32).
    total_sum is GENERATED ALWAYS AS (quantity * rate_at_time) STORED (§33).
    """

    __tablename__ = "work_entries"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_work_entries_quantity"),
        CheckConstraint("rate_at_time >= 0", name="chk_work_entries_rate"),
        CheckConstraint(
            "status IN ('DRAFT', 'CONFIRMED', 'PAID')",
            name="chk_work_entries_status",
        ),
        CheckConstraint(
            "status <> 'PAID' OR payroll_period_id IS NOT NULL",
            name="chk_work_entries_paid_period",
        ),
        # §64 — mandatory index name.
        Index("idx_work_entries_emp", "employee_id", "entry_date", "status"),
        Index("idx_work_entries_ip_date", "ip_id", "entry_date"),
        Index("idx_work_entries_order", "order_id"),
        Index("idx_work_entries_type", "work_operation_type_id", "entry_date"),
        Index(
            "idx_work_entries_period",
            "payroll_period_id",
            postgresql_where=text("payroll_period_id IS NOT NULL"),
        ),
        # §41 / ADR-010 — race-safe Excel import idempotency.
        Index(
            "uq_work_entries_import_fingerprint",
            "ip_id",
            "import_fingerprint",
            unique=True,
            postgresql_where=text("import_fingerprint IS NOT NULL"),
        ),
        {"comment": "Piecework entries — the labour part of direct cost (MASTER PROMPT §33, §37)."},
    )

    id: Mapped[int] = pk_column()
    employee_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    work_operation_type_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("work_operation_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    entry_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    quantity: Mapped[decimal.Decimal] = quantity_column()
    rate_at_time: Mapped[decimal.Decimal] = mapped_column(
        MONEY,
        nullable=False,
        comment="FROZEN rate copied from rates_history on INSERT; "
        "immutable afterwards (§32, ADR-008).",
    )
    total_sum: Mapped[decimal.Decimal] = mapped_column(
        MONEY,
        Computed("quantity * rate_at_time", persisted=True),
        nullable=True,
        comment="GENERATED ALWAYS AS (quantity * rate_at_time) STORED (§33).",
    )
    created_by_user: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'DRAFT'"),
        comment="DRAFT | CONFIRMED | PAID (§33).",
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    # production additions (§81)
    payroll_period_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("payroll_periods.id", ondelete="RESTRICT"),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    import_batch_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("import_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    import_fingerprint: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    employee: Mapped[Employee] = relationship(back_populates="work_entries")
    work_operation_type: Mapped[WorkOperationType] = relationship()
    order: Mapped[Order | None] = relationship(back_populates="work_entries")
    payroll_period: Mapped[PayrollPeriod | None] = relationship()
    import_batch: Mapped[ImportBatch | None] = relationship()


class PayrollSummary(Base):
    """
    Payroll calculation per employee per period (§35).

    total_calculated is GENERATED:
        piecework_total + base_salary + bonuses - deductions
    """

    __tablename__ = "payroll_summary"
    __table_args__ = (
        CheckConstraint("piecework_total >= 0", name="chk_payroll_summary_piecework"),
        CheckConstraint("base_salary >= 0", name="chk_payroll_summary_base"),
        CheckConstraint("bonuses >= 0", name="chk_payroll_summary_bonuses"),
        CheckConstraint("deductions >= 0", name="chk_payroll_summary_deductions"),
        CheckConstraint(
            "status IN ('CALCULATED', 'APPROVED', 'PAID', 'CANCELLED')",
            name="chk_payroll_summary_status",
        ),
        # §57 — one summary per employee per period; prevents double payroll.
        UniqueConstraint(
            "employee_id",
            "payroll_period_id",
            name="uq_payroll_summary_employee_period",
        ),
        Index("idx_payroll_summary_period", "payroll_period_id", "status"),
        Index("idx_payroll_summary_employee", "employee_id"),
        # P2-2 FK index (declared to match schema.sql).
        Index("idx_payroll_summary_ip", "ip_id"),
        {"comment": "Payroll calculation per employee per period (MASTER PROMPT §35)."},
    )

    id: Mapped[int] = pk_column()
    employee_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payroll_period_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("payroll_periods.id", ondelete="RESTRICT"),
        nullable=False,
    )
    piecework_total: Mapped[decimal.Decimal] = money_column(default_zero=True)
    base_salary: Mapped[decimal.Decimal] = money_column(default_zero=True)
    bonuses: Mapped[decimal.Decimal] = money_column(default_zero=True)
    deductions: Mapped[decimal.Decimal] = money_column(default_zero=True)
    total_calculated: Mapped[decimal.Decimal] = mapped_column(
        MONEY,
        Computed(
            "piecework_total + base_salary + bonuses - deductions",
            persisted=True,
        ),
        nullable=True,
        comment="GENERATED: piecework_total + base_salary + bonuses - deductions (§35).",
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    # production additions (§81)
    ip_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'CALCULATED'"),
    )
    calculated_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    employee: Mapped[Employee] = relationship()
    payroll_period: Mapped[PayrollPeriod] = relationship()
    payments: Mapped[list[PayrollPayment]] = relationship(
        back_populates="payroll_summary",
        passive_deletes="all",
    )


class PayrollPayment(Base):
    """Salary payment linked to the financial ledger (§36)."""

    __tablename__ = "payroll_payments"
    __table_args__ = (
        CheckConstraint(
            "payment_method IN ('KASPI_TRANSFER', 'CASH')",
            name="chk_payroll_payments_method",
        ),
        CheckConstraint("amount > 0", name="chk_payroll_payments_amount"),
        CheckConstraint(
            "status IN ('POSTED', 'REVERSED')",
            name="chk_payroll_payments_status",
        ),
        # §57 — one ledger operation can back only one salary payment.
        UniqueConstraint("operation_id", name="uq_payroll_payments_operation"),
        Index("idx_payroll_payments_summary", "payroll_summary_id"),
        Index("idx_payroll_payments_date", "payment_date"),
        # §57 — at most one POSTED payment per payroll summary (race-safe).
        Index(
            "uq_payroll_payments_active",
            "payroll_summary_id",
            unique=True,
            postgresql_where=text("status = 'POSTED'"),
        ),
        {"comment": "Salary payments linked to the financial ledger (MASTER PROMPT §36)."},
    )

    id: Mapped[int] = pk_column()
    payroll_summary_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("payroll_summary.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    amount: Mapped[decimal.Decimal] = money_column()
    payment_method: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="KASPI_TRANSFER | CASH (§36).",
    )
    operation_id: Mapped[int | None] = mapped_column(
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
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    payroll_summary: Mapped[PayrollSummary] = relationship(back_populates="payments")
    operation: Mapped[Operation | None] = relationship()


__all__ = [
    "Employee",
    "PayrollPayment",
    "PayrollPeriod",
    "PayrollSummary",
    "RateHistory",
    "WorkEntry",
    "WorkOperationType",
]
