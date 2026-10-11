"""PAY-001: Un-approve payroll summary — closes dead-end workflow."""

import pytest
from sqlalchemy import text


def test_unapprove_reverses_approval(engine, applied_schema):
    """APPROVED → CALCULATED → can recalculate."""
    import random

    from sqlalchemy.orm import Session

    from app.services import (
        approve_payroll_summary,
        calculate_payroll,
        unapprove_payroll_summary,
    )

    bin_code = f"{random.randint(100000000000, 999999999999)}"[:12]
    with Session(engine) as session:
        # Setup: IP, employee, period, work entry
        ip_id = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('PAY-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

        emp_id = session.execute(
            text("""
                INSERT INTO employees (full_name, assigned_ip_id, payment_type)
                VALUES ('Test Worker', :ip, 'PIECEWORK') RETURNING id
            """),
            {"ip": ip_id},
        ).scalar_one()

        wot_id = session.execute(
            text("SELECT id FROM work_operation_types LIMIT 1")
        ).scalar_one()

        period_id = session.execute(
            text("""
                INSERT INTO payroll_periods (start_date, end_date, status)
                VALUES ('2026-01-01', '2026-01-31', 'OPEN') RETURNING id
            """)
        ).scalar_one()

        # Work entry
        session.execute(
            text("""
                INSERT INTO work_entries
                (ip_id, employee_id, work_operation_type_id, entry_date, quantity, status)
                VALUES (:ip, :emp, :wot, '2026-01-15', 10, 'CONFIRMED')
            """),
            {"ip": ip_id, "emp": emp_id, "wot": wot_id},
        )
        session.commit()

        # Calculate → APPROVED
        summaries = calculate_payroll(session, period_id, {}, ip_ids=[ip_id])
        assert len(summaries) > 0
        summary_id = summaries[0]["id"]

        approved = approve_payroll_summary(session, summary_id)
        assert approved["status"] == "APPROVED"
        session.commit()

        # Un-approve → CALCULATED
        unapproved = unapprove_payroll_summary(session, summary_id)
        assert unapproved["status"] == "CALCULATED"
        session.commit()

        # Can recalculate now (was blocked when APPROVED)
        summaries2 = calculate_payroll(session, period_id, {}, ip_ids=[ip_id])
        assert len(summaries2) > 0


def test_unapprove_paid_fails(engine, applied_schema):
    """Cannot un-approve a PAID summary."""
    import random

    from sqlalchemy.orm import Session

    from app.services import unapprove_payroll_summary

    bin_code = f"{random.randint(100000000000, 999999999999)}"[:12]
    with Session(engine) as session:
        ip_id = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('PAY-TEST2', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

        # Create a PAID summary directly
        emp_id = session.execute(
            text("""
                INSERT INTO employees (full_name, assigned_ip_id, payment_type)
                VALUES ('Test Worker 2', :ip, 'PIECEWORK') RETURNING id
            """),
            {"ip": ip_id},
        ).scalar_one()

        period_id = session.execute(
            text("""
                INSERT INTO payroll_periods (start_date, end_date, status)
                VALUES ('2026-02-01', '2026-02-28', 'OPEN') RETURNING id
            """)
        ).scalar_one()

        summary_id = session.execute(
            text("""
                INSERT INTO payroll_summary
                (payroll_period_id, employee_id, ip_id, piecework_total, status)
                VALUES (:p, :e, :ip, 1000, 'PAID') RETURNING id
            """),
            {"p": period_id, "e": emp_id, "ip": ip_id},
        ).scalar_one()
        session.commit()

        with pytest.raises(ValueError, match="PAID"):
            unapprove_payroll_summary(session, summary_id)
