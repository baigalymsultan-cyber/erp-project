"""GAP-001: layered P&L + D1 (GAP-004): explicit-null accrual_date contract."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.schemas import OperationCreate, PayrollPeriodCreate
from app.services import add_operation, pnl


@pytest.fixture
def pnl_world(engine, applied_schema):
    import random

    bin_code = f"{random.randint(100000000000, 999999999999)}"[:12]
    with Session(engine) as session:
        ip_id = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('PNL-TEST', :b) RETURNING id"),
            {"b": bin_code},
        ).scalar_one()

        type_ids = {
            row[0]: row[1]
            for row in session.execute(text("SELECT code, id FROM operation_types")).all()
        }

        def post(code, amount, accrual="2026-01-15", status="POSTED"):
            session.execute(
                text(
                    """
                    INSERT INTO operations
                    (ip_id, type_id, operation_date, accrual_date, amount, status)
                    VALUES (:ip, :t, '2026-01-15', :acc, :amt, :st)
                    """
                ),
                {"ip": ip_id, "t": type_ids[code], "acc": accrual, "amt": amount, "st": status},
            )

        # Layer fixture: revenue 1_000_000; direct 250_000; salary 300_000
        post("SALE_KASPI_SHOP", 1000000)
        post("EXPENSE_MATERIAL", -200000)
        post("EXPENSE_KASPI_FEE", -50000)
        post("SALARY_PAYMENT", -300000)
        # Exclusions: DRAFT, pure settlement (NULL accrual).
        # (Transfer-leg exclusion is covered by test_financial_regression_*;
        # transfer legs can only be created via the atomic transfer endpoint.)
        post("EXPENSE_MATERIAL", -99999, status="DRAFT")
        post("EXPENSE_MATERIAL", -77777, accrual=None)
        session.commit()

    return {"ip_id": ip_id}


def test_pnl_layered_keys(engine, applied_schema, pnl_world):
    """GAP-001: layered breakdown with reconciliation invariants."""
    with Session(engine) as session:
        r = pnl(session, ip_ids=[pnl_world["ip_id"]])

    # Flat keys unchanged (backward compatible)
    assert r["total_income"] == Decimal("1000000.00")
    assert r["total_expense"] == Decimal("550000.00")
    assert r["net_profit"] == Decimal("450000.00")

    # Layered keys
    assert r["revenue"] == Decimal("1000000.00")
    assert r["direct_cost"] == Decimal("250000.00")  # MATERIAL + KASPI_FEE
    assert r["gross_margin"] == Decimal("750000.00")
    assert r["operating_expenses"] == Decimal("300000.00")  # SALARY_PAYMENT
    assert r["operating_result"] == Decimal("450000.00")
    assert r["salary_unclassified"] == Decimal("300000.00")
    assert r["other_income"] == Decimal("0.00")
    assert r["other_expenses"] == Decimal("0.00")
    assert r["net_result"] == Decimal("450000.00")

    # Reconciliation invariants: layers must tie to flat totals
    assert r["direct_cost"] + r["operating_expenses"] == r["total_expense"]
    assert r["net_result"] == r["net_profit"]
    assert r["revenue"] == r["total_income"]
    assert r["gross_margin"] == r["revenue"] - r["direct_cost"]
    assert r["operating_result"] == r["gross_margin"] - r["operating_expenses"]


def test_pnl_layered_period_basis(engine, applied_schema, pnl_world):
    """P&L stays accrual-basis: cross-period accrual excluded."""
    with Session(engine) as session:
        feb = PayrollPeriodCreate(period_start=date(2026, 2, 1), period_end=date(2026, 2, 28))
        r = pnl(session, period=feb, ip_ids=[pnl_world["ip_id"]])
    assert r["revenue"] == Decimal("0.00")
    assert r["net_result"] == Decimal("0.00")


def test_pnl_layered_api(engine, applied_schema, pnl_world):
    """GET /pnl exposes layered keys (VIEWER privilege)."""
    from fastapi.testclient import TestClient

    from app.api import app
    from app.core.security import hash_password

    w = pnl_world
    with Session(engine) as session:
        pw_hash = hash_password("testpw123")
        user_id = session.execute(
            text("INSERT INTO users (username, password_hash) VALUES (:u, :h) RETURNING id"),
            {"u": f"pnl-admin-{w['ip_id']}", "h": pw_hash},
        ).scalar_one()
        role_id = session.execute(text("SELECT id FROM roles WHERE code = 'ADMIN'")).scalar_one()
        session.execute(
            text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
            {"u": user_id, "r": role_id},
        )
        session.commit()

    client = TestClient(app)
    with client:
        r = client.post(
            "/auth/login",
            json={"username": f"pnl-admin-{w['ip_id']}", "password": "testpw123"},
        )
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]

        resp = client.get(
            "/pnl",
            params={
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "ip_id": w["ip_id"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in (
        "revenue",
        "direct_cost",
        "gross_margin",
        "operating_expenses",
        "operating_result",
        "net_result",
    ):
        assert key in body, f"missing layered key {key}"
    assert body["net_result"] == body["net_profit"]
    assert body["revenue"] == "1000000.00"


# --------------------------------------------------------------------------- #
# D1 (GAP-004): add_operation must honor the explicit-null accrual contract.
# --------------------------------------------------------------------------- #


def _op_data(session, ip_id, **overrides):
    type_id = session.execute(
        text("SELECT id FROM operation_types WHERE code = 'EXPENSE_MATERIAL'")
    ).scalar_one()
    base = {
        "ip_id": ip_id,
        "type_id": type_id,
        "direction": "OUT",
        "amount": Decimal("-1000.00"),
        "operation_date": date(2026, 3, 10),
    }
    base.update(overrides)
    return OperationCreate(**base)


def test_d1_omitted_accrual_defaults_to_operation_date(engine, applied_schema):
    """Omitted accrual_date → cash basis (operation_date)."""
    with Session(engine) as session:
        ip_id = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('D1-A', '111111111111') RETURNING id")
        ).scalar_one()
        op_id = add_operation(session, _op_data(session, ip_id))
        session.commit()
        acc = session.execute(
            text("SELECT accrual_date FROM operations WHERE id = :i"), {"i": op_id}
        ).scalar_one()
    assert acc == date(2026, 3, 10)


def test_d1_explicit_null_accrual_stays_null(engine, applied_schema):
    """Explicit None → pure settlement: accrual_date stays NULL (no P&L effect)."""
    with Session(engine) as session:
        ip_id = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('D1-B', '222222222222') RETURNING id")
        ).scalar_one()
        op_id = add_operation(session, _op_data(session, ip_id, accrual_date=None))
        session.commit()
        acc = session.execute(
            text("SELECT accrual_date FROM operations WHERE id = :i"), {"i": op_id}
        ).scalar_one()
    assert acc is None


def test_d1_explicit_null_excluded_from_pnl(engine, applied_schema):
    """A pure-settlement op must not hit P&L."""
    with Session(engine) as session:
        ip_id = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES ('D1-C', '333333333333') RETURNING id")
        ).scalar_one()
        add_operation(session, _op_data(session, ip_id, accrual_date=None))
        session.commit()
        r = pnl(
            session,
            period=PayrollPeriodCreate(period_start=date(2026, 3, 1), period_end=date(2026, 3, 31)),
            ip_ids=[ip_id],
        )
    assert r["total_expense"] == Decimal("0.00")
    assert r["net_result"] == Decimal("0.00")
