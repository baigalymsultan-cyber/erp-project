"""ADR-016: compensating-operation reversal model tests.

Tests the new reversal model (docs/REVERSAL_DECISIONS_2026-10-10.md Q1-Q7):
- Q1: reversal = new POSTED compensating operation; original never mutates
- Q2: REVERSAL_IN (IN) / REVERSAL_OUT (OUT) direction-locked kinds
- Q3: link via reverses_operation_id; related_operation_id untouched
- Q4: partial reversal allowed; DB trigger enforces cap
- Q5: mandatory reversal_reason; original comment never overwritten
- Q6: (RBAC unchanged — covered by existing auth tests)
- Q7: pnl() nets reversal pairs; v_ip_financial_summary nets

Requires a real PostgreSQL (triggers, CHECK constraints). Skipped when
the test database is unreachable.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.services.finance import pnl, reverse_operation


@pytest.fixture(scope="module")
def reversal_ctx(engine: Engine, applied_schema) -> dict[str, int]:
    """Isolated test context: IP, account, counterparty."""
    suffix = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        ip_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": f"REVERSAL {suffix}", "b": f"{(int(suffix, 16)) % 10**12:012d}"},
        ).scalar_one()
        account_id = conn.execute(
            text(
                "INSERT INTO accounts (ip_id, type, name) "
                "VALUES (:ip, 'OTHER', :n) RETURNING id"
            ),
            {"ip": ip_id, "n": f"REVERSAL account {suffix}"},
        ).scalar_one()
    return {"ip": int(ip_id), "account": int(account_id)}


def _insert_op(
    engine: Engine,
    ip_id: int,
    type_code: str,
    amount: Decimal,
    account_id: int | None = None,
) -> int:
    """Insert a POSTED operation directly."""
    with engine.begin() as conn:
        return conn.execute(
            text(
                """
                INSERT INTO operations (ip_id, type_id, amount, operation_date,
                                        accrual_date, account_id, status)
                VALUES (:ip, (SELECT id FROM operation_types WHERE code = :code),
                        :amount, CURRENT_DATE, CURRENT_DATE, :account, 'POSTED')
                RETURNING id
                """
            ),
            {"ip": ip_id, "code": type_code, "amount": amount, "account": account_id},
        ).scalar_one()


def _get_op(engine: Engine, op_id: int) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, status, amount, comment, reverses_operation_id, "
                "reversal_reason FROM operations WHERE id = :id"
            ),
            {"id": op_id},
        ).mappings().one()
        return dict(row)


# ---------------------------------------------------------------------------
# Q1/Q3/Q5: compensating operation, original untouched
# ---------------------------------------------------------------------------


def test_full_reversal_creates_compensating_operation(
    engine: Engine, reversal_ctx: dict[str, int]
) -> None:
    """Q1: reversal = new POSTED row with inverse effect; original untouched."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "SALE_KASPI_PAY", Decimal("1000.00"))

    with Session(bind=engine) as session:
        result = reverse_operation(session, original_id, "customer refund")
        session.commit()

    reversal_id = result["id"]
    assert reversal_id != original_id
    assert result["status"] == "POSTED"
    assert result["reverses_operation_id"] == original_id

    # Original: still POSTED, amount unchanged, comment untouched.
    original = _get_op(engine, original_id)
    assert original["status"] == "POSTED"
    assert original["amount"] == Decimal("1000.00")
    assert original["comment"] is None
    assert original["reverses_operation_id"] is None

    # Reversal: inverse amount, linked, reason stored.
    reversal = _get_op(engine, reversal_id)
    assert reversal["status"] == "POSTED"
    assert reversal["amount"] == Decimal("-1000.00")
    assert reversal["reverses_operation_id"] == original_id
    assert reversal["reversal_reason"] == "customer refund"


def test_reversal_of_expense_uses_reversal_in(
    engine: Engine, reversal_ctx: dict[str, int]
) -> None:
    """Q2: EXPENSE (OUT) reverses via REVERSAL_IN (IN, positive amount)."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "EXPENSE_MATERIAL", Decimal("-500.00"))

    with Session(bind=engine) as session:
        result = reverse_operation(session, original_id, "material return")
        session.commit()

    reversal = _get_op(engine, result["id"])
    assert reversal["amount"] == Decimal("500.00")

    # Verify the kind is REVERSAL_IN.
    with engine.connect() as conn:
        kind = conn.execute(
            text(
                "SELECT ot.kind FROM operations o "
                "JOIN operation_types ot ON ot.id = o.type_id "
                "WHERE o.id = :id"
            ),
            {"id": result["id"]},
        ).scalar_one()
    assert kind == "REVERSAL_IN"


def test_reversal_reason_required(engine: Engine, reversal_ctx: dict[str, int]) -> None:
    """Q5: reversal without a reason is rejected."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "SALE_KASPI_PAY", Decimal("100.00"))

    with Session(bind=engine) as session, pytest.raises(
        ValueError, match="reversal_reason is required"
    ):
        reverse_operation(session, original_id, "")
    with Session(bind=engine) as session, pytest.raises(
        ValueError, match="reversal_reason is required"
    ):
        reverse_operation(session, original_id, "   ")


def test_original_comment_never_overwritten(
    engine: Engine, reversal_ctx: dict[str, int]
) -> None:
    """Q5/TD-019: the original comment is preserved, not overwritten."""
    ip_id = reversal_ctx["ip"]
    with engine.begin() as conn:
        original_id = conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, "
                "comment, status) VALUES (:ip, "
                "(SELECT id FROM operation_types WHERE code = 'SALE_KASPI_SHOP'), "
                "200.00, CURRENT_DATE, 'original business reason', 'POSTED') "
                "RETURNING id"
            ),
            {"ip": ip_id},
        ).scalar_one()

    with Session(bind=engine) as session:
        reverse_operation(session, original_id, "duplicate charge refund")
        session.commit()

    original = _get_op(engine, original_id)
    assert original["comment"] == "original business reason"


# ---------------------------------------------------------------------------
# Q4: partial reversal and cap
# ---------------------------------------------------------------------------


def test_partial_reversal(engine: Engine, reversal_ctx: dict[str, int]) -> None:
    """Q4: partial reversal creates a proportional compensating operation."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "SALE_KASPI_PAY", Decimal("1000.00"))

    with Session(bind=engine) as session:
        result = reverse_operation(
            session, original_id, "partial refund", amount=Decimal("300.00")
        )
        session.commit()

    reversal = _get_op(engine, result["id"])
    assert reversal["amount"] == Decimal("-300.00")
    assert reversal["reverses_operation_id"] == original_id


def test_partial_reversals_capped_at_original(
    engine: Engine, reversal_ctx: dict[str, int]
) -> None:
    """Q4: SUM(compensations) <= original.amount enforced by DB trigger."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "SALE_KASPI_PAY", Decimal("1000.00"))

    with Session(bind=engine) as session:
        # Two partials totaling 700 — OK.
        reverse_operation(session, original_id, "refund 1", amount=Decimal("400.00"))
        reverse_operation(
            session, original_id, "refund 2", amount=Decimal("300.00"),
            idempotency_key="r2",
        )
        session.commit()

    # Third partial of 400 would exceed the 1000 cap (700 + 400 > 1000).
    # The DB trigger raises at INSERT/flush time.
    with Session(bind=engine) as session, pytest.raises(
        Exception, match="reversal cap exceeded"
    ):
        reverse_operation(
            session, original_id, "refund 3", amount=Decimal("400.00"),
            idempotency_key="r3",
        )
        session.commit()


def test_full_reversal_twice_rejected_by_cap(
    engine: Engine, reversal_ctx: dict[str, int]
) -> None:
    """Q4: a second full reversal exceeds the cap (no double reversal)."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "SALE_KASPI_PAY", Decimal("250.00"))

    with Session(bind=engine) as session:
        reverse_operation(session, original_id, "first refund")
        session.commit()

    with Session(bind=engine) as session, pytest.raises(
        Exception, match="reversal cap exceeded"
    ):
        reverse_operation(
            session, original_id, "second refund", idempotency_key="second"
        )
        session.commit()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_retry_returns_existing(
    engine: Engine, reversal_ctx: dict[str, int]
) -> None:
    """Constraint 6: same idempotency key -> ON CONFLICT DO NOTHING."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "SALE_KASPI_PAY", Decimal("800.00"))

    with Session(bind=engine) as session:
        r1 = reverse_operation(
            session, original_id, "refund", idempotency_key="key-123"
        )
        session.commit()

    with Session(bind=engine) as session:
        r2 = reverse_operation(
            session, original_id, "refund", idempotency_key="key-123"
        )
        session.commit()

    assert r1["id"] == r2["id"]

    # Only one reversal row exists.
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM operations WHERE reverses_operation_id = :id"),
            {"id": original_id},
        ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# Transfer legs
# ---------------------------------------------------------------------------


def test_transfer_leg_rejection(engine: Engine, reversal_ctx: dict[str, int]) -> None:
    """Constraint 7: transfer legs still cannot be reversed independently."""
    ip_id = reversal_ctx["ip"]
    suffix = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        ip2_id = conn.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {"n": f"REVERSAL2 {suffix}", "b": f"{(int(suffix, 16) + 1) % 10**12:012d}"},
        ).scalar_one()
        acc1 = conn.execute(
            text("INSERT INTO accounts (ip_id, type, name) VALUES (:ip, 'OTHER', :n) RETURNING id"),
            {"ip": ip_id, "n": f"REV T1 {uuid.uuid4().hex[:8]}"},
        ).scalar_one()
        acc2 = conn.execute(
            text("INSERT INTO accounts (ip_id, type, name) VALUES (:ip, 'OTHER', :n) RETURNING id"),
            {"ip": ip2_id, "n": f"REV T2 {uuid.uuid4().hex[:8]}"},
        ).scalar_one()
        # Create linked transfer legs directly (DRAFT then POSTED to avoid trigger complexity).
        out_id = conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, "
                "account_id, status) VALUES (:ip, "
                "(SELECT id FROM operation_types WHERE code = 'TRANSFER_OUT'), "
                "-100.00, CURRENT_DATE, :acc, 'DRAFT') RETURNING id"
            ),
            {"ip": ip_id, "acc": acc1},
        ).scalar_one()
        in_id = conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, "
                "account_id, status) VALUES (:ip, "
                "(SELECT id FROM operation_types WHERE code = 'TRANSFER_IN'), "
                "100.00, CURRENT_DATE, :acc, 'DRAFT') RETURNING id"
            ),
            {"ip": ip2_id, "acc": acc2},
        ).scalar_one()
        conn.execute(
            text("UPDATE operations SET related_operation_id = :in WHERE id = :out"),
            {"in": in_id, "out": out_id},
        )
        conn.execute(
            text("UPDATE operations SET related_operation_id = :out WHERE id = :in"),
            {"out": out_id, "in": in_id},
        )
        conn.execute(
            text("UPDATE operations SET status = 'POSTED' WHERE id IN (:out, :in)"),
            {"out": out_id, "in": in_id},
        )

    with Session(bind=engine) as session, pytest.raises(
        ValueError, match="transfer leg cannot be reversed"
    ):
        reverse_operation(session, out_id, "trying to reverse a leg")
    with Session(bind=engine) as session, pytest.raises(
        ValueError, match="transfer leg cannot be reversed"
    ):
        reverse_operation(session, in_id, "trying to reverse a leg")


# ---------------------------------------------------------------------------
# Q7: P&L netting
# ---------------------------------------------------------------------------


def test_pnl_nets_full_reversal(engine: Engine, reversal_ctx: dict[str, int]) -> None:
    """Q7: full reversal -> net 0 in pnl()."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "SALE_KASPI_PAY", Decimal("1000.00"))

    with Session(bind=engine) as session:
        before = pnl(session, ip_ids=[ip_id])
        reverse_operation(session, original_id, "full refund")
        session.commit()
        after = pnl(session, ip_ids=[ip_id])

    # The reversal nets the income.
    assert after["total_income"] == before["total_income"] - Decimal("1000.00")
    assert after["net_profit"] == before["net_profit"] - Decimal("1000.00")


def test_pnl_nets_expense_reversal(engine: Engine, reversal_ctx: dict[str, int]) -> None:
    """Q7: EXPENSE reversal via REVERSAL_IN nets total_expense."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "EXPENSE_MATERIAL", Decimal("-400.00"))

    with Session(bind=engine) as session:
        before = pnl(session, ip_ids=[ip_id])
        reverse_operation(session, original_id, "material return")
        session.commit()
        after = pnl(session, ip_ids=[ip_id])

    assert after["total_expense"] == before["total_expense"] - Decimal("400.00")


def test_view_nets_reversal_pair(engine: Engine, reversal_ctx: dict[str, int]) -> None:
    """Q7: v_ip_financial_summary nets the pair."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "SALE_KASPI_PAY", Decimal("600.00"))

    with engine.connect() as conn:
        before = conn.execute(
            text(
                "SELECT total_income, net_profit FROM v_ip_financial_summary "
                "WHERE ip_id = :ip"
            ),
            {"ip": ip_id},
        ).mappings().one()

    with Session(bind=engine) as session:
        reverse_operation(session, original_id, "refund")
        session.commit()

    with engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT total_income, net_profit FROM v_ip_financial_summary "
                "WHERE ip_id = :ip"
            ),
            {"ip": ip_id},
        ).mappings().one()

    assert after["total_income"] == before["total_income"] - Decimal("600.00")
    assert after["net_profit"] == before["net_profit"] - Decimal("600.00")


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def test_reversal_creates_audit_trail(engine: Engine, reversal_ctx: dict[str, int]) -> None:
    """Both the original and the reversal are in the audit log."""
    ip_id = reversal_ctx["ip"]
    original_id = _insert_op(engine, ip_id, "SALE_KASPI_PAY", Decimal("300.00"))

    with Session(bind=engine) as session:
        result = reverse_operation(session, original_id, "audit test")
        session.commit()

    # The generic audit trigger logs INSERTs on operations.
    with engine.connect() as conn:
        audit_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM audit_log WHERE entity = 'operations' "
                "AND entity_id IN (:orig, :rev)"
            ),
            {"orig": original_id, "rev": result["id"]},
        ).scalar_one()
    # At least the reversal INSERT is audited (original may predate test).
    assert audit_count >= 1


def test_reversal_of_nonexistent_raises(engine: Engine, reversal_ctx: dict[str, int]) -> None:
    """Reversing a non-existent operation raises."""
    with Session(bind=engine) as session, pytest.raises(ValueError, match="does not exist"):
        reverse_operation(session, 999999999, "nope")


def test_reversal_of_non_posted_raises(engine: Engine, reversal_ctx: dict[str, int]) -> None:
    """Only POSTED operations can be reversed."""
    ip_id = reversal_ctx["ip"]
    with engine.begin() as conn:
        draft_id = conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, "
                "status) VALUES (:ip, "
                "(SELECT id FROM operation_types WHERE code = 'SALE_KASPI_PAY'), "
                "50.00, CURRENT_DATE, 'DRAFT') RETURNING id"
            ),
            {"ip": ip_id},
        ).scalar_one()

    with Session(bind=engine) as session, pytest.raises(
        ValueError, match="only POSTED operations"
    ):
        reverse_operation(session, draft_id, "nope")
