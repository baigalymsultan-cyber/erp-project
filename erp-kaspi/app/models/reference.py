"""
Reference / master-data models.

1:1 mapping of database/schema.sql:
  ip_entities        [ORIGINAL #1,  §17]
  operation_types    [ORIGINAL #3,  §20]
  categories         [ORIGINAL #4,  §21]
  counterparties     [ORIGINAL #5,  §22]
  accounts           [ARCHITECTURAL EXTENSION, §51]
  products           [ARCHITECTURAL EXTENSION, §46]
  materials          [ARCHITECTURAL EXTENSION, §48]
  tax_rates_history  [ARCHITECTURAL EXTENSION, §75]
"""

from __future__ import annotations

import datetime
import decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
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
    TAX_RATE,
    TIMESTAMPTZ,
    Base,
    bool_column,
    created_at_column,
    money_column,
    pk_column,
    updated_at_column,
)

if TYPE_CHECKING:
    from app.models.finance import Operation


class IpEntity(Base):
    """Individual entrepreneur (ИП) the system keeps books for (§17)."""

    __tablename__ = "ip_entities"
    __table_args__ = (
        CheckConstraint("length(bin) = 12", name="chk_ip_entities_bin_length"),
        CheckConstraint("bin ~ '^[0-9]{12}$'", name="chk_ip_entities_bin_digits"),
        CheckConstraint("length(btrim(name)) > 0", name="chk_ip_entities_name"),
        CheckConstraint(
            "closed_at IS NULL OR closed_at >= created_at",
            name="chk_ip_entities_closed_at",
        ),
        CheckConstraint(
            "NOT (is_active AND closed_at IS NOT NULL)",
            name="chk_ip_entities_active_state",
        ),
        {
            "comment": "Individual entrepreneurs (ИП) the system keeps books for "
            "(MASTER PROMPT §17)."
        },
    )

    id: Mapped[int] = pk_column()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    bin: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        unique=True,
        comment="БИН — exactly 12 digits, unique (§17).",
    )
    is_active: Mapped[bool] = bool_column(default=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    closed_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMPTZ,
        nullable=True,
        comment="Date the ИП was closed; NULL while operating.",
    )
    # production additions (§81 — original columns preserved)
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    accounts: Mapped[list[Account]] = relationship(
        back_populates="ip_entity",
        passive_deletes="all",
    )


class OperationType(Base):
    """Financial operation type; direction drives the amount sign (§20, §27)."""

    __tablename__ = "operation_types"
    __table_args__ = (
        CheckConstraint("direction IN ('IN', 'OUT')", name="chk_operation_types_direction"),
        CheckConstraint(
            "kind IN ('INCOME', 'EXPENSE', 'TRANSFER', 'REVERSAL_IN', 'REVERSAL_OUT')",
            name="chk_operation_types_kind",
        ),
        CheckConstraint(
            "(kind = 'TRANSFER' AND affects_pnl = FALSE) OR (kind <> 'TRANSFER')",
            name="chk_operation_types_pnl",
        ),
        CheckConstraint(
            "(kind = 'INCOME'      AND direction = 'IN')  OR "
            "(kind = 'EXPENSE'     AND direction = 'OUT') OR "
            "(kind = 'REVERSAL_IN' AND direction = 'IN')  OR "
            "(kind = 'REVERSAL_OUT' AND direction = 'OUT') OR "
            "(kind = 'TRANSFER')",
            name="chk_operation_types_dir_kind",
        ),
        {
            "comment": "Financial operation types; direction drives amount sign "
            "(MASTER PROMPT §20, §27)."
        },
    )

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    affects_pnl: Mapped[bool] = bool_column(
        default=True,
        comment="FALSE for transfers between ИП — not income, not expense (§28, §76).",
    )
    is_system: Mapped[bool] = bool_column(
        default=False,
        comment="TRUE for the 7 mandatory types from §20 — must not be deleted or renamed.",
    )
    is_active: Mapped[bool] = bool_column(default=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()


class Category(Base):
    """Income / expense category (§21)."""

    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("kind IN ('INCOME', 'EXPENSE')", name="chk_categories_kind"),
        CheckConstraint("length(btrim(name)) > 0", name="chk_categories_name"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="chk_categories_no_self"),
        UniqueConstraint("name", "kind", name="uq_categories_name_kind"),
        {"comment": "Income and expense categories (MASTER PROMPT §21)."},
    )

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="INCOME | EXPENSE — must match operation_types.kind of the operation (§21).",
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
    )
    is_system: Mapped[bool] = bool_column(default=False)
    is_active: Mapped[bool] = bool_column(default=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    parent: Mapped[Category | None] = relationship(remote_side="Category.id")


class Counterparty(Base):
    """Supplier, customer or other counterparty (§22)."""

    __tablename__ = "counterparties"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="chk_counterparties_name"),
        CheckConstraint(
            "bin_iin IS NULL OR bin_iin ~ '^[0-9]{12}$'",
            name="chk_counterparties_bin_iin",
        ),
        {"comment": "Suppliers, customers and other counterparties (MASTER PROMPT §22)."},
    )

    id: Mapped[int] = pk_column()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    bin_iin: Mapped[str | None] = mapped_column(
        String(12),
        nullable=True,
        comment="БИН/ИИН — 12 digits when known (§22).",
    )
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    # production additions (§81 — original columns preserved)
    is_active: Mapped[bool] = bool_column(default=True)
    updated_at: Mapped[datetime.datetime] = updated_at_column()


class Account(Base):
    """ARCHITECTURAL EXTENSION — money account per ИП (§51)."""

    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "type IN ('KASPI_PAY', 'KASPI_SHOP', 'BANK', 'CASH', 'OTHER')",
            name="chk_accounts_type",
        ),
        CheckConstraint("length(btrim(name)) > 0", name="chk_accounts_name"),
        UniqueConstraint("ip_id", "name", name="uq_accounts_ip_name"),
        Index("uq_accounts_id_ip", "id", "ip_id", unique=True),
        Index("idx_accounts_ip", "ip_id", "type"),
        {"comment": "ARCHITECTURAL EXTENSION — money accounts per ИП (MASTER PROMPT §51)."},
    )

    id: Mapped[int] = pk_column()
    ip_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # G-BANK-1: bank identifier for type=BANK accounts (HALYK, FREEDOM,
    # KASPI, ...). NULL for non-bank accounts. Added by migration 0017.
    bank_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = bool_column(default=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    ip_entity: Mapped[IpEntity] = relationship(back_populates="accounts")
    operations: Mapped[list[Operation]] = relationship(
        back_populates="account",
        foreign_keys="Operation.account_id",
        passive_deletes="all",
    )


class Product(Base):
    """ARCHITECTURAL EXTENSION — product catalogue (§46)."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("length(btrim(sku)) > 0", name="chk_products_sku"),
        CheckConstraint("length(btrim(name)) > 0", name="chk_products_name"),
        CheckConstraint("selling_price >= 0", name="chk_products_selling_price"),
        Index("idx_products_active", "is_active", postgresql_where=text("is_active")),
        {"comment": "ARCHITECTURAL EXTENSION — product catalogue (MASTER PROMPT §46)."},
    )

    id: Mapped[int] = pk_column()
    sku: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'шт'"),
    )
    selling_price: Mapped[decimal.Decimal] = money_column(default_zero=True)
    is_active: Mapped[bool] = bool_column(default=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()


class Material(Base):
    """ARCHITECTURAL EXTENSION — materials catalogue (§48)."""

    __tablename__ = "materials"
    __table_args__ = (
        CheckConstraint("length(btrim(sku)) > 0", name="chk_materials_sku"),
        CheckConstraint("length(btrim(name)) > 0", name="chk_materials_name"),
        CheckConstraint("length(btrim(unit)) > 0", name="chk_materials_unit"),
        CheckConstraint("current_price >= 0", name="chk_materials_current_price"),
        Index("idx_materials_supplier", "supplier_id"),
        Index("idx_materials_active", "is_active", postgresql_where=text("is_active")),
        {"comment": "ARCHITECTURAL EXTENSION — materials catalogue (MASTER PROMPT §48)."},
    )

    id: Mapped[int] = pk_column()
    sku: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    current_price: Mapped[decimal.Decimal] = money_column(
        nullable=False,
        comment="Current price. Historical cost is frozen in order_materials."
        "price_at_time (§38).",
    )
    supplier_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("counterparties.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_active: Mapped[bool] = bool_column(default=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()

    supplier: Mapped[Counterparty | None] = relationship()


class TaxRateHistory(Base):
    """
    ARCHITECTURAL EXTENSION — historised tax parameters (§75).

    Values are intentionally NOT seeded: the applicable tax regime is an open
    question (OQ-001) and §94 forbids inventing external rules.
    """

    __tablename__ = "tax_rates_history"
    __table_args__ = (
        CheckConstraint("rate >= 0", name="chk_tax_rates_history_rate"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="chk_tax_rates_history_period",
        ),
        CheckConstraint(
            "base IN ('REVENUE', 'PROFIT', 'PAYROLL', 'FIXED')",
            name="chk_tax_rates_history_base",
        ),
        ExcludeConstraint(
            ("code", "="),
            (text("coalesce(ip_id, 0)"), "="),
            (text("tstzrange(effective_from, effective_to, '[)')"), "&&"),
            name="excl_tax_rates_history_overlap",
            using="gist",
        ),
        Index("idx_tax_rates_lookup", "code", "effective_from", "effective_to"),
        {
            "comment": "ARCHITECTURAL EXTENSION — historised tax parameters; never "
            "hard-coded (MASTER PROMPT §75). Values pending OQ-001."
        },
    )

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rate: Mapped[decimal.Decimal] = mapped_column(TAX_RATE, nullable=False)
    base: Mapped[str] = mapped_column(String(50), nullable=False)
    ip_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("ip_entities.id", ondelete="RESTRICT"),
        nullable=True,
    )
    effective_from: Mapped[datetime.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    effective_to: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    legal_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = created_at_column()
    updated_at: Mapped[datetime.datetime] = updated_at_column()


__all__ = [
    "Account",
    "Category",
    "Counterparty",
    "IpEntity",
    "Material",
    "OperationType",
    "Product",
    "TaxRateHistory",
]
