"""DI-001..DI-005: settlement_links integrity triggers."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def settlement_world(engine, applied_schema):
    """Two IPs, operations for testing settlement_links integrity."""
    import random

    from sqlalchemy.orm import Session

    bin1 = f"{random.randint(100000000000, 999999999999)}"[:12]
    bin2 = f"{random.randint(100000000000, 999999999999)}"[:12]

    with Session(engine) as session:
        ip1 = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('SETL-IP1', :b) RETURNING id"),
            {"b": bin1},
        ).scalar_one()
        ip2 = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('SETL-IP2', :b) RETURNING id"),
            {"b": bin2},
        ).scalar_one()

        exp_type = session.execute(
            text("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
        ).scalar_one()

        # Accrual operation (IP1, POSTED, with accrual_date)
        accrual_id = session.execute(
            text("""
                INSERT INTO operations
                (ip_id, type_id, operation_date, accrual_date, amount, status)
                VALUES (:ip, :t, '2026-01-15', '2026-01-15', -500000, 'POSTED')
                RETURNING id
            """),
            {"ip": ip1, "t": exp_type},
        ).scalar_one()

        # Payment operation (IP1, POSTED)
        payment_id = session.execute(
            text("""
                INSERT INTO operations
                (ip_id, type_id, operation_date, accrual_date, amount, status)
                VALUES (:ip, :t, '2026-02-10', NULL, -500000, 'POSTED')
                RETURNING id
            """),
            {"ip": ip1, "t": exp_type},
        ).scalar_one()

        # Operation in IP2 (for cross-IP test)
        ip2_op = session.execute(
            text("""
                INSERT INTO operations
                (ip_id, type_id, operation_date, accrual_date, amount, status)
                VALUES (:ip, :t, '2026-01-15', '2026-01-15', -1000, 'POSTED')
                RETURNING id
            """),
            {"ip": ip2, "t": exp_type},
        ).scalar_one()

        # DRAFT operation (for status gate test)
        draft_id = session.execute(
            text("""
                INSERT INTO operations
                (ip_id, type_id, operation_date, accrual_date, amount, status)
                VALUES (:ip, :t, '2026-01-15', '2026-01-15', -1000, 'DRAFT')
                RETURNING id
            """),
            {"ip": ip1, "t": exp_type},
        ).scalar_one()

        session.commit()

    yield {
        "ip1": ip1, "ip2": ip2,
        "accrual": accrual_id, "payment": payment_id,
        "ip2_op": ip2_op, "draft": draft_id,
    }
    # Cleanup handled by conftest _purge_business_rows (settlement_links in purge order)


def test_valid_link_succeeds(engine, applied_schema, settlement_world):
    """Happy path: same IP, POSTED, accrual has date, amount within cap."""
    from sqlalchemy.orm import Session

    w = settlement_world
    with Session(engine) as session:
        link_id = session.execute(
            text("""
                INSERT INTO settlement_links
                (ip_id, accrual_operation_id, payment_operation_id, allocated_amount)
                VALUES (:ip, :acc, :pay, 500000) RETURNING id
            """),
            {"ip": w["ip1"], "acc": w["accrual"], "pay": w["payment"]},
        ).scalar_one()
        session.commit()
        assert link_id is not None


def test_di001_cross_ip_blocked(engine, applied_schema, settlement_world):
    """DI-001: Cannot link operations from different IPs."""
    from sqlalchemy.orm import Session

    w = settlement_world
    with Session(engine) as session, pytest.raises(IntegrityError, match="cross-IP"):
        session.execute(
            text("""
                    INSERT INTO settlement_links
                    (ip_id, accrual_operation_id, payment_operation_id, allocated_amount)
                    VALUES (:ip, :acc, :pay, 1000)
                """),
            {"ip": w["ip1"], "acc": w["accrual"], "pay": w["ip2_op"]},
        )
        session.commit()


def test_di004_draft_blocked(engine, applied_schema, settlement_world):
    """DI-004: Cannot link DRAFT operations."""
    from sqlalchemy.orm import Session

    w = settlement_world
    with Session(engine) as session, pytest.raises(IntegrityError, match="must be POSTED"):
        session.execute(
            text("""
                    INSERT INTO settlement_links
                    (ip_id, accrual_operation_id, payment_operation_id, allocated_amount)
                    VALUES (:ip, :acc, :pay, 1000)
                """),
            {"ip": w["ip1"], "acc": w["draft"], "pay": w["payment"]},
        )
        session.commit()


def test_di003_over_allocation_blocked(engine, applied_schema, settlement_world):
    """DI-003: Cannot allocate more than accrual amount."""
    from sqlalchemy.orm import Session

    w = settlement_world
    with Session(engine) as session:
        # First allocate 400000 (within 500000 cap)
        session.execute(
            text("""
                INSERT INTO settlement_links
                (ip_id, accrual_operation_id, payment_operation_id, allocated_amount)
                VALUES (:ip, :acc, :pay, 400000)
            """),
            {"ip": w["ip1"], "acc": w["accrual"], "pay": w["payment"]},
        )
        session.commit()

        # Try to allocate another 200000 (total 600000 > 500000) → blocked
        with pytest.raises(IntegrityError, match="over-allocation"):
            session.execute(
                text("""
                    INSERT INTO settlement_links
                    (ip_id, accrual_operation_id, payment_operation_id, allocated_amount)
                    VALUES (:ip, :acc, :pay, 200000)
                """),
                {"ip": w["ip1"], "acc": w["accrual"], "pay": w["payment"]},
            )
            session.commit()


def test_di005_immutable(engine, applied_schema, settlement_world):
    """DI-005: settlement_links rows cannot be UPDATE/DELETE."""
    from sqlalchemy.orm import Session

    w = settlement_world
    with Session(engine) as session:
        link_id = session.execute(
            text("""
                INSERT INTO settlement_links
                (ip_id, accrual_operation_id, payment_operation_id, allocated_amount)
                VALUES (:ip, :acc, :pay, 100000) RETURNING id
            """),
            {"ip": w["ip1"], "acc": w["accrual"], "pay": w["payment"]},
        ).scalar_one()
        session.commit()

        # UPDATE blocked
        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                text("UPDATE settlement_links SET allocated_amount = 200000 WHERE id = :id"),
                {"id": link_id},
            )
            session.commit()
        session.rollback()

        # DELETE blocked
        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                text("DELETE FROM settlement_links WHERE id = :id"),
                {"id": link_id},
            )
            session.commit()
        session.rollback()
        # Note: fixture cleanup handles link deletion
