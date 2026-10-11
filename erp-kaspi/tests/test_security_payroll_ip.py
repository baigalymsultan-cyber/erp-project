"""Security P1 regressions: SEC-003, SEC-002(b), DI-005 bypass, DI-003 race."""

import threading

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import hash_password

#: Deterministic month allocator for payroll periods: the sec_world fixture
#: commits its period (function-scoped), and the overlap EXCLUDE constraint
#: is global — two invocations must never share a month. A random draw
#: collided 1/8 of the time; a counter cannot.
_SEC_WORLD_MONTH = iter(range(1, 13))


def _make_user(session, username, role_code, employee_id=None):
    user_id = session.execute(
        text(
            "INSERT INTO users (username, password_hash, employee_id) "
            "VALUES (:u, :h, :e) RETURNING id"
        ),
        {"u": username, "h": hash_password("testpw123"), "e": employee_id},
    ).scalar_one()
    role_id = session.execute(
        text("SELECT id FROM roles WHERE code = :c"), {"c": role_code}
    ).scalar_one()
    session.execute(
        text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
        {"u": user_id, "r": role_id},
    )
    session.commit()
    return username


def _login(client, username):
    r = client.post("/auth/login", json={"username": username, "password": "testpw123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def sec_world(engine, applied_schema):
    import random

    with Session(engine) as session:
        b1 = f"{random.randint(100000000000, 999999999999)}"[:12]
        b2 = f"{random.randint(100000000000, 999999999999)}"[:12]
        ip1 = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('SEC-IP1', :b) RETURNING id"),
            {"b": b1},
        ).scalar_one()
        ip2 = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('SEC-IP2', :b) RETURNING id"),
            {"b": b2},
        ).scalar_one()
        # Employees: one per IP (for IP-restricted users) + payroll worker at IP1
        emp_ip1 = session.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary) "
                "VALUES ('Acct1', :ip, 'SALARY', 0) RETURNING id"
            ),
            {"ip": ip1},
        ).scalar_one()
        emp_ip2 = session.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary) "
                "VALUES ('Acct2', :ip, 'SALARY', 0) RETURNING id"
            ),
            {"ip": ip2},
        ).scalar_one()
        worker = session.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
                "VALUES ('Worker', :ip, 'PIECEWORK') RETURNING id"
            ),
            {"ip": ip1},
        ).scalar_one()
        director_emp = session.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary) "
                "VALUES ('Dir', :ip, 'SALARY', 0) RETURNING id"
            ),
            {"ip": ip1},
        ).scalar_one()

        acct1 = _make_user(session, f"sec-acct1-{ip1}", "ACCOUNTANT", emp_ip1)
        acct2 = _make_user(session, f"sec-acct2-{ip2}", "ACCOUNTANT", emp_ip2)
        director = _make_user(session, f"sec-dir-{ip1}", "DIRECTOR", director_emp)

        # Payroll world: period + work entry + calculated summary (ip_id=IP1).
        # Unique period per run: the §94 purge runs at session end, so two
        # tests must not reuse the same range (uq_payroll_periods_range).
        # Year 2027: no other test file creates periods there (session purge
        # runs at the very end, so ranges must be globally unique).
        # Deterministic month from a counter — the fixture commits, so a
        # random draw could collide with the other test's committed period.
        month = next(_SEC_WORLD_MONTH)
        if month == 3:  # test_sec003 creates 2027-03 via API
            month = next(_SEC_WORLD_MONTH)
        p_start, p_end = f"2027-{month:02d}-01", f"2027-{month:02d}-28"
        period_id = session.execute(
            text(
                "INSERT INTO payroll_periods (start_date, end_date, status) "
                "VALUES (:s, :e, 'OPEN') RETURNING id"
            ),
            {"s": p_start, "e": p_end},
        ).scalar_one()
        wot = session.execute(text("SELECT id FROM work_operation_types LIMIT 1")).scalar_one()
        session.execute(
            text(
                "INSERT INTO work_entries (ip_id, employee_id, work_operation_type_id, "
                "entry_date, quantity, status, rate_at_time) "
                "VALUES (:ip, :e, :w, :d, 10, 'CONFIRMED', 1000)"
            ),
            {"ip": ip1, "e": worker, "w": wot, "d": f"2027-{month:02d}-15"},
        )
        session.commit()

        from app.services import calculate_payroll

        summaries = calculate_payroll(session, period_id, {}, ip_ids=[ip1])
        assert len(summaries) == 1
        summary_id = summaries[0]["id"]
        frozen_ip = session.execute(
            text("SELECT ip_id FROM payroll_summary WHERE id = :i"), {"i": summary_id}
        ).scalar_one()
        assert frozen_ip == ip1, "calculate must freeze ip_id"
        session.commit()

    return {
        "ip1": ip1,
        "ip2": ip2,
        "worker": worker,
        "acct1": acct1,
        "acct2": acct2,
        "director": director,
        "summary_id": summary_id,
    }


def test_sec003_period_creation_requires_director(engine, applied_schema, sec_world):
    """SEC-003 P1: IP-restricted ACCOUNTANT must not create global payroll periods."""
    from fastapi.testclient import TestClient

    from app.api import app

    w = sec_world
    with TestClient(app) as client:
        h_acct = _login(client, w["acct1"])
        r = client.post(
            "/payroll/periods",
            json={"period_start": "2027-03-01", "period_end": "2027-03-31"},
            headers=h_acct,
        )
        assert r.status_code == 403, r.text

        h_dir = _login(client, w["director"])
        r = client.post(
            "/payroll/periods",
            json={"period_start": "2027-03-01", "period_end": "2027-03-31"},
            headers=h_dir,
        )
        assert r.status_code == 201, r.text


def test_sec002b_stale_ip_approval_blocked(engine, applied_schema, sec_world):
    """SEC-002(b) P1: reassignment must not transfer approval power."""
    from fastapi.testclient import TestClient

    from app.api import app

    w = sec_world
    with TestClient(app) as client:
        h_acct2 = _login(client, w["acct2"])
        # IP2 accountant cannot approve IP1's summary even before reassignment
        r = client.post(f"/payroll/summaries/{w['summary_id']}/approve", headers=h_acct2)
        assert r.status_code == 403, r.text

    # Reassign the worker to IP2 (the stale-IP scenario)
    with Session(engine) as session:
        session.execute(
            text("UPDATE employees SET assigned_ip_id = :ip WHERE id = :e"),
            {"ip": w["ip2"], "e": w["worker"]},
        )
        session.commit()

    with TestClient(app) as client:
        h_acct2 = _login(client, w["acct2"])
        h_acct1 = _login(client, w["acct1"])
        # Still 403: authorization is pinned to the frozen summary ip_id
        r = client.post(f"/payroll/summaries/{w['summary_id']}/approve", headers=h_acct2)
        assert r.status_code == 403, r.text
        # IP1's accountant retains approval power
        r = client.post(f"/payroll/summaries/{w['summary_id']}/approve", headers=h_acct1)
        assert r.status_code == 200, r.text


def test_di005_bypass_blocked_for_app_role(engine, applied_schema):
    """DI-005 HIGH: GUC alone must not permit settlement_links mutation."""
    import random

    with Session(engine) as session:
        b = f"{random.randint(100000000000, 999999999999)}"[:12]
        ip = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('DI5', :b) RETURNING id"),
            {"b": b},
        ).scalar_one()
        t = session.execute(
            text("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
        ).scalar_one()
        acc = session.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, operation_date, "
                "accrual_date, amount, status) "
                "VALUES (:ip, :t, '2026-01-15', '2026-01-15', -1000, 'POSTED') RETURNING id"
            ),
            {"ip": ip, "t": t},
        ).scalar_one()
        pay = session.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, operation_date, "
                "accrual_date, amount, status) "
                "VALUES (:ip, :t, '2026-02-10', NULL, -1000, 'POSTED') RETURNING id"
            ),
            {"ip": ip, "t": t},
        ).scalar_one()
        link = session.execute(
            text(
                "INSERT INTO settlement_links (ip_id, accrual_operation_id, "
                "payment_operation_id, allocated_amount) VALUES (:ip, :a, :p, 1000) RETURNING id"
            ),
            {"ip": ip, "a": acc, "p": pay},
        ).scalar_one()
        session.commit()

        # Attacker session (erp_app, NOT erp_migrator): set the GUC and try DELETE
        session.execute(text("SET app.allow_financial_delete = 'on'"))
        with pytest.raises(Exception, match=r"(immutable|restrict_violation|check_violation)"):
            session.execute(text("DELETE FROM settlement_links WHERE id = :i"), {"i": link})
            session.flush()
        session.rollback()

        # Row survives
        n = session.execute(
            text("SELECT count(*) FROM settlement_links WHERE id = :i"), {"i": link}
        ).scalar_one()
        assert n == 1


def test_di003_concurrent_overallocation_blocked(engine, applied_schema):
    """DI-003: two concurrent inserts exceeding the cap — one must fail."""
    import random

    with Session(engine) as session:
        b = f"{random.randint(100000000000, 999999999999)}"[:12]
        ip = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('RACE', :b) RETURNING id"),
            {"b": b},
        ).scalar_one()
        t = session.execute(
            text("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
        ).scalar_one()
        acc = session.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, operation_date, "
                "accrual_date, amount, status) "
                "VALUES (:ip, :t, '2026-01-15', '2026-01-15', -1000, 'POSTED') RETURNING id"
            ),
            {"ip": ip, "t": t},
        ).scalar_one()
        pay = session.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, operation_date, "
                "accrual_date, amount, status) "
                "VALUES (:ip, :t, '2026-02-10', NULL, -1000, 'POSTED') RETURNING id"
            ),
            {"ip": ip, "t": t},
        ).scalar_one()
        session.commit()

    barrier = threading.Barrier(2)
    results = {}

    def try_insert(name):
        conn = engine.connect()
        trans = conn.begin()
        try:
            barrier.wait(timeout=10)
            conn.execute(
                text(
                    "INSERT INTO settlement_links (ip_id, accrual_operation_id, "
                    "payment_operation_id, allocated_amount) VALUES (:ip, :a, :p, 600)"
                ),
                {"ip": ip, "a": acc, "p": pay},
            )
            trans.commit()
            results[name] = "ok"
        except Exception as exc:
            trans.rollback()
            results[name] = f"fail: {type(exc).__name__}"
        finally:
            conn.close()

    t1 = threading.Thread(target=try_insert, args=("t1",))
    t2 = threading.Thread(target=try_insert, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert sorted(results.values()) == ["fail: IntegrityError", "ok"], results
    with Session(engine) as session:
        total = session.execute(
            text(
                "SELECT COALESCE(SUM(allocated_amount),0) FROM settlement_links "
                "WHERE accrual_operation_id = :a"
            ),
            {"a": acc},
        ).scalar_one()
    assert total == 600, "cap must hold under concurrency"
