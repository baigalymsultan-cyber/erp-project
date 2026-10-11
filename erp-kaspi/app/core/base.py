"""
SQLAlchemy 2.x declarative base and shared column helpers.

MASTER PROMPT references:
  §7  — stack: SQLAlchemy 2.x, Alembic, PostgreSQL 15+.
  §13 — money is NUMERIC(15,2); no floating point anywhere.
  §14 — quantity is NUMERIC(15,3).
  §15 — all timestamps are TIMESTAMPTZ, stored in UTC.
  §56 — every mutable table carries created_at / updated_at.

INVARIANT (ADR-011): database/schema.sql is the single source of truth for
the physical schema. The models in this package are a 1:1 mapping of that
file and are verified against the applied database by
tests/test_models_match_schema.py. A model must never introduce, rename or
drop a database object on its own — the DDL is changed first, then the model
is aligned with it.
"""

from __future__ import annotations

import datetime
import decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, MetaData, Numeric, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------
# Naming convention
#
# The convention below reproduces the names that PostgreSQL generated for the
# constraints declared inline in database/schema.sql:
#   PRIMARY KEY  -> <table>_pkey
#   UNIQUE       -> <table>_<col>_key
#   FOREIGN KEY  -> <table>_<col>_fkey
# CHECK constraints and multi-column UNIQUE constraints are named explicitly
# in the DDL (chk_*, uq_*, excl_*) and are therefore always passed by name in
# the models as well.
# ---------------------------------------------------------------------------
NAMING_CONVENTION: dict[str, str] = {
    "pk": "%(table_name)s_pkey",
    "uq": "%(table_name)s_%(column_0_name)s_key",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "ix": "idx_%(table_name)s_%(column_0_name)s",
    # NOTE: no "ck" entry on purpose. Every CHECK constraint in schema.sql
    # carries an explicit chk_* name, and a "ck" convention containing the
    # %(constraint_name)s token would be applied on top of that explicit name,
    # producing chk_<table>_chk_<table>_<rule>. Omitting the key keeps the
    # names exactly as declared in the model and in the DDL.
}

metadata_obj = MetaData(schema=None, naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Declarative base for every ERP model."""

    metadata = metadata_obj

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        pk_columns = [c.name for c in self.__table__.primary_key.columns]  # type: ignore[attr-defined]
        values = ", ".join(f"{name}={getattr(self, name, None)!r}" for name in pk_columns)
        return f"<{type(self).__name__} {values}>"


# ---------------------------------------------------------------------------
# Shared column factories
#
# These helpers exist so the physical type of a money / quantity / timestamp
# column cannot drift between tables (§13, §14, §15).
# ---------------------------------------------------------------------------

#: NUMERIC(15,2) — every monetary amount in KZT (§13).
MONEY = Numeric(15, 2)

#: NUMERIC(15,3) — every quantity (m2, running metres, hours, pieces) (§14).
QUANTITY = Numeric(15, 3)

#: NUMERIC(9,6) — tax rate (fraction, e.g. 0.030000 for 3 %) (§75).
TAX_RATE = Numeric(9, 6)

#: TIMESTAMPTZ — all points in time are stored with a time zone (§15).
TIMESTAMPTZ = DateTime(timezone=True)


def pk_column() -> Mapped[int]:
    """BIGSERIAL PRIMARY KEY."""
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


def created_at_column() -> Mapped[datetime.datetime]:
    """created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP (§56)."""
    return mapped_column(
        TIMESTAMPTZ,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


def updated_at_column() -> Mapped[datetime.datetime]:
    """
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP (§56).

    The value is maintained by the database trigger fn_set_updated_at();
    the application never writes it explicitly.
    """
    return mapped_column(
        TIMESTAMPTZ,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


def bool_column(
    *,
    default: bool,
    nullable: bool = False,
    comment: str | None = None,
) -> Mapped[bool]:
    """BOOLEAN NOT NULL DEFAULT <default>."""
    return mapped_column(
        Boolean,
        nullable=nullable,
        server_default=text("TRUE" if default else "FALSE"),
        comment=comment,
    )


def money_column(
    *,
    nullable: bool = False,
    default_zero: bool = False,
    comment: str | None = None,
) -> Mapped[decimal.Decimal]:
    """NUMERIC(15,2) money column (§13)."""
    kwargs: dict[str, Any] = {"nullable": nullable}
    if default_zero:
        kwargs["server_default"] = text("0")
    if comment is not None:
        kwargs["comment"] = comment
    return mapped_column(MONEY, **kwargs)


def quantity_column(*, nullable: bool = False) -> Mapped[decimal.Decimal]:
    """NUMERIC(15,3) quantity column (§14)."""
    return mapped_column(QUANTITY, nullable=nullable)


def counter_column() -> Mapped[int]:
    """INTEGER NOT NULL DEFAULT 0 — ETL / sync counters."""
    from sqlalchemy import Integer

    return mapped_column(Integer, nullable=False, server_default=text("0"))


__all__ = [
    "MONEY",
    "NAMING_CONVENTION",
    "QUANTITY",
    "TAX_RATE",
    "TIMESTAMPTZ",
    "Base",
    "bool_column",
    "counter_column",
    "created_at_column",
    "metadata_obj",
    "money_column",
    "pk_column",
    "quantity_column",
    "updated_at_column",
]
