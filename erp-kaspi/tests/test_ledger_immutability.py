"""FIN-OPS-0001 — posted-ledger immutability for NON-transfer operations.

FINANCE-OPS-001 §7: after posting, amount, direction (type), legal entity,
account, source/document identity and actor must not change through UPDATE;
corrections go through reversal / correction / refund / adjustment.

Transfer legs keep their own guard (ADR-015, tests/test_transfer_ledger.py) and
are intentionally out of scope here. Every rejected UPDATE is asserted to come
from fn_protect_posted_ledger_operation() (message contains "FIN-OPS-0001"),
so a regression in that trigger cannot hide behind another constraint.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = ROOT / "database" / "schema.sql"
MIGRATION_FILE = (
    ROOT / "alembic" / "versions" / "20260926_1500_0007_posted_ledger_immutability.py"
)

#: (type code, same-direction alternative type, same-kind category, sample amount)
NON_TRANSFER_TYPES: tuple[tuple[str, str, str, Decimal], ...] = (
    ("SALE_KASPI_SHOP", "SALE_KASPI_PAY", "INCOME_OTHER", Decimal("100.00")),
    ("SALE_KASPI_PAY", "SALE_KASPI_SHOP", "INCOME_OTHER", Decimal("100.00")),
    ("EXPENSE_MATERIAL", "EXPENSE_KASPI_FEE", "EXPENSE_OTHER", Decimal("-100.00")),
    ("EXPENSE_KASPI_FEE", "EXPENSE_MATERIAL", "EXPENSE_OTHER", Decimal("-100.00")),
    ("SALARY_PAYMENT", "EXPENSE_MATERIAL", "EXPENSE_OTHER", Decimal("-100.00")),
)

#: Protected column -> SET expression. Each expression keeps every OTHER
#: constraint satisfied (sign, category kind, account/IP FK, uniqueness), so
#: the only reason for rejection is the FIN-OPS-0001 trigger.
PROTECTED_MUTATIONS: dict[str, str] = {
    "amount": "amount = amount * 2",
    "operation_date": "operation_date = operation_date - 1",
    "ip_id": "ip_id = :other_ip",
    "type_id": "type_id = (SELECT id FROM operation_types WHERE code = :alt_type)",
    "category_id": "category_id = (SELECT id FROM categories WHERE code = :category)",
    "counterparty_id": "counterparty_id = :counterparty",
    "account_id": "account_id = :account",
    "external_id": "external_id = 'FINOPS1-' || id::text",
    "related_operation_id": "related_operation_id = :sibling",
    "import_fingerprint": "import_fingerprint = encode(sha256(id::text::bytea), 'hex')",
    "created_at": "created_at = created_at - interval '1 day'",
}


@pytest.fixture(scope="module")
def ledger_ctx(engine: Engine, applied_schema) -> dict[str, int]:
    suffix = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        ips = []
        for number in (1, 2):
            ips.append(
                conn.execute(
                    text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
                    {
                        "n": f"FINOPS1 {suffix} {number}",
                        "b": f"{(int(suffix, 16) + number) % 10**12:012d}",
                    },
                ).scalar_one()
            )
        account = conn.execute(
            text(
                "INSERT INTO accounts (ip_id, type, name) "
                "VALUES (:ip, 'OTHER', :n) RETURNING id"
            ),
            {"ip": ips[0], "n": f"FINOPS1 account {suffix}"},
        ).scalar_one()
        counterparty = conn.execute(
            text("INSERT INTO counterparties (name) VALUES (:n) RETURNING id"),
            {"n": f"FINOPS1 counterparty {suffix}"},
        ).scalar_one()
    return {
        "ip": int(ips[0]),
        "other_ip": int(ips[1]),
        "account": int(account),
        "counterparty": int(counterparty),
    }


def _insert_operation(
    engine: Engine,
    ip_id: int,
    type_code: str,
    amount: Decimal,
    status: str = "POSTED",
) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO operations (ip_id, type_id, amount, operation_date, "
                    "comment, status) VALUES (:ip, "
                    "(SELECT id FROM operation_types WHERE code = :code), "
                    ":amount, CURRENT_DATE, 'FIN-OPS-0001 test', :status) RETURNING id"
                ),
                {"ip": ip_id, "code": type_code, "amount": amount, "status": status},
            ).scalar_one()
        )


def _assert_rejected_by_ledger_guard(
    engine: Engine, sql: str, params: dict[str, object]
) -> None:
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            with pytest.raises(IntegrityError) as excinfo:
                conn.execute(text(sql), params)
        finally:
            transaction.rollback()
    assert "FIN-OPS-0001" in str(excinfo.value), (
        f"rejected by a different constraint, not the ledger guard: {excinfo.value}"
    )


def _status(engine: Engine, operation_id: int) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM operations WHERE id = :id"), {"id": operation_id}
            ).scalar_one()
        )


# --------------------------------------------------------------------------- #
# Posted financial fields are immutable for every non-transfer type
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("column", sorted(PROTECTED_MUTATIONS))
@pytest.mark.parametrize(
    ("type_code", "alt_type", "category", "amount"),
    NON_TRANSFER_TYPES,
    ids=[row[0] for row in NON_TRANSFER_TYPES],
)
def test_posted_operation_financial_field_is_immutable(
    engine: Engine,
    ledger_ctx: dict[str, int],
    type_code: str,
    alt_type: str,
    category: str,
    amount: Decimal,
    column: str,
) -> None:
    operation_id = _insert_operation(engine, ledger_ctx["ip"], type_code, amount)
    sibling_id = _insert_operation(engine, ledger_ctx["ip"], type_code, amount)

    _assert_rejected_by_ledger_guard(
        engine,
        f"UPDATE operations SET {PROTECTED_MUTATIONS[column]} WHERE id = :id",
        {
            "id": operation_id,
            "other_ip": ledger_ctx["other_ip"],
            "alt_type": alt_type,
            "category": category,
            "counterparty": ledger_ctx["counterparty"],
            "account": ledger_ctx["account"],
            "sibling": sibling_id,
        },
    )

    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT amount, status FROM operations WHERE id = :id"),
            {"id": operation_id},
        ).one()
    assert stored == (amount, "POSTED")


def test_reversed_operation_financial_fields_are_immutable(
    engine: Engine, ledger_ctx: dict[str, int]
) -> None:
    operation_id = _insert_operation(engine, ledger_ctx["ip"], "SALE_KASPI_SHOP", Decimal("55.10"))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE operations SET status = 'REVERSED' WHERE id = :id"),
            {"id": operation_id},
        )
    _assert_rejected_by_ledger_guard(
        engine, "UPDATE operations SET amount = 1.00 WHERE id = :id", {"id": operation_id}
    )


# --------------------------------------------------------------------------- #
# Status transitions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("start", "target"),
    [
        ("POSTED", "DRAFT"),
        ("POSTED", "CORRECTED"),
        ("REVERSED", "POSTED"),
        ("REVERSED", "DRAFT"),
        ("REVERSED", "CORRECTED"),
        ("DRAFT", "REVERSED"),
        ("DRAFT", "CORRECTED"),
    ],
)
def test_invalid_status_transition_is_rejected(
    engine: Engine, ledger_ctx: dict[str, int], start: str, target: str
) -> None:
    insert_status = "DRAFT" if start == "DRAFT" else "POSTED"
    operation_id = _insert_operation(
        engine, ledger_ctx["ip"], "EXPENSE_MATERIAL", Decimal("-42.00"), insert_status
    )
    if start == "REVERSED":
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE operations SET status = 'REVERSED' WHERE id = :id"),
                {"id": operation_id},
            )

    _assert_rejected_by_ledger_guard(
        engine,
        "UPDATE operations SET status = :target WHERE id = :id",
        {"id": operation_id, "target": target},
    )
    assert _status(engine, operation_id) == start


def test_posted_to_reversed_is_still_allowed(engine: Engine, ledger_ctx: dict[str, int]) -> None:
    """The current reverse_operation() edge keeps working until ADR-016."""
    operation_id = _insert_operation(engine, ledger_ctx["ip"], "SALE_KASPI_PAY", Decimal("10.00"))
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE operations SET status = 'REVERSED', comment = 'reversal reason' "
                "WHERE id = :id"
            ),
            {"id": operation_id},
        )
    assert _status(engine, operation_id) == "REVERSED"


def test_draft_is_editable_and_can_be_posted(engine: Engine, ledger_ctx: dict[str, int]) -> None:
    operation_id = _insert_operation(
        engine, ledger_ctx["ip"], "EXPENSE_KASPI_FEE", Decimal("-5.00"), "DRAFT"
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE operations SET amount = -7.50, operation_date = operation_date - 1, "
                "account_id = :account, counterparty_id = :counterparty WHERE id = :id"
            ),
            {
                "id": operation_id,
                "account": ledger_ctx["account"],
                "counterparty": ledger_ctx["counterparty"],
            },
        )
        conn.execute(
            text("UPDATE operations SET status = 'POSTED' WHERE id = :id"),
            {"id": operation_id},
        )
    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT amount, status FROM operations WHERE id = :id"), {"id": operation_id}
        ).one()
    assert stored == (Decimal("-7.50"), "POSTED")


def test_posted_comment_stays_mutable_until_adr016(
    engine: Engine, ledger_ctx: dict[str, int]
) -> None:
    """Documented gap (TD-019): reverse_operation() still overwrites comment."""
    operation_id = _insert_operation(engine, ledger_ctx["ip"], "SALE_KASPI_SHOP", Decimal("1.00"))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE operations SET comment = 'edited' WHERE id = :id"), {"id": operation_id}
        )


# --------------------------------------------------------------------------- #
# Schema / migration consistency
# --------------------------------------------------------------------------- #


def _function_body(source: str) -> str:
    match = re.search(
        r"CREATE OR REPLACE FUNCTION fn_protect_posted_ledger_operation\(\).*?\n\$\$;\n",
        source,
        re.S,
    )
    assert match is not None
    return match.group(0)


def test_migration_function_matches_schema(engine: Engine, applied_schema) -> None:
    """ADR-011: schema.sql is the source of truth; DB function must match it.

    Note: Compares the LIVE database function (after all migrations) with
    schema.sql, not the historical 0007 migration file. The 0007 file is
    immutable history; later migrations (e.g. 0015 adding accrual_date)
    legitimately evolve the function. What matters is DB == schema.sql.
    """
    with engine.connect() as conn:
        db_body = conn.execute(
            text(
                "SELECT pg_get_functiondef('fn_protect_posted_ledger_operation()'::regprocedure)"
            )
        ).scalar_one()
    # Normalize: pg_get_functiondef returns CREATE OR REPLACE FUNCTION ...
    # Compare the body (from BEGIN to END) ignoring formatting differences
    def _normalize_body(sql: str) -> str:
        # Extract between BEGIN and END; (handles both $$ and $function$)
        m = re.search(r"\bBEGIN\b(.*?)\bEND\b", sql, re.S | re.I)
        if not m:
            return sql
        body = m.group(1)
        # Normalize: lowercase, collapse whitespace, remove empty lines
        lines = [" ".join(line.strip().split()) for line in body.strip().split("\n")]
        lines = [line.lower() for line in lines if line]
        return "\n".join(lines)

    schema_body = _function_body(SCHEMA_FILE.read_text(encoding="utf-8"))
    assert _normalize_body(db_body) == _normalize_body(schema_body), (
        "DB fn_protect_posted_ledger_operation() does not match schema.sql"
    )


def test_ledger_guard_trigger_is_installed_and_enabled(engine: Engine, applied_schema) -> None:
    with engine.connect() as conn:
        enabled = conn.execute(
            text(
                "SELECT tgenabled FROM pg_trigger "
                "WHERE tgrelid = 'public.operations'::regclass "
                "AND tgname = 'trg_operations_protect_posted_ledger' AND NOT tgisinternal"
            )
        ).scalar_one_or_none()
    assert enabled in ("O", "A"), f"trigger missing or disabled: {enabled!r}"
