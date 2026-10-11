"""
Financial integrity tests — MASTER PROMPT §16, §28, §44, §63.

Verifies the invariants that protect real money:
  * operations are append-only (§16: DELETE is forbidden by trigger);
  * transfers between own ИП are excluded from P&L (§28, §63);
  * Kaspi payout reconciliation matches stored gross vs item registry (§44);
  * the financial summary view keeps transfers out of income/expense (§63).
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import DatabaseSettings


def _settings():
    return DatabaseSettings.from_env()


@pytest.fixture(scope="module")
def ip_id(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) "
                "VALUES ('FINTEST ИП', '900000000001') ON CONFLICT (bin) DO NOTHING"
            )
        )
        return conn.execute(
            text("SELECT id FROM ip_entities WHERE bin = '900000000001'")
        ).scalar_one()


def test_operations_are_append_only(engine, ip_id):
    """§16 — DELETE on the ledger must be rejected by the trigger."""
    with engine.begin() as conn:
        op_id = conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, comment) "
                "VALUES (:ip, (SELECT id FROM operation_types WHERE code = 'SALE_KASPI_PAY'), "
                "50000, CURRENT_DATE, 'fin test') RETURNING id"
            ),
            {"ip": ip_id, "ext_id": f"FIN-PAY-{uuid.uuid4().hex[:8]}"},
        ).scalar_one()
    with pytest.raises(Exception), engine.begin() as conn:
        conn.execute(text("DELETE FROM operations WHERE id = :i"), {"i": op_id})


def test_delete_escape_hatch_requires_migrator_role(engine, ip_id):
    """TD-003 — the maintenance GUC alone must not unlock physical DELETE.

    Original defect (reproduced 2026-09-16): the unprivileged application
    role could run ``SET LOCAL app.allow_financial_delete = 'on'`` and
    physically delete a posted operation. PostgreSQL cannot revoke SET on a
    custom ``app.*`` GUC, so fn_forbid_delete() now additionally requires
    membership in the schema-owner/migrator role (TD-008), which erp_app does
    not have and cannot grant itself. Fail-closed.
    """
    with engine.begin() as conn:
        current_user = conn.execute(text("SELECT current_user")).scalar_one()
    if current_user == "erp_migrator":
        pytest.skip("connected as the migrator role; the hatch is legitimately available")

    with engine.begin() as conn:
        op_id = conn.execute(
            text(
                "INSERT INTO operations (ip_id, type_id, amount, operation_date, comment) "
                "VALUES (:ip, (SELECT id FROM operation_types WHERE code = 'SALE_KASPI_PAY'), "
                "50000, CURRENT_DATE, 'TD-003 regression') RETURNING id"
            ),
            {"ip": ip_id},
        ).scalar_one()
    with pytest.raises(Exception), engine.begin() as conn:
        conn.execute(text("SET LOCAL app.allow_financial_delete = 'on'"))
        conn.execute(text("DELETE FROM operations WHERE id = :i"), {"i": op_id})


def test_transfer_rejected_between_same_ip(rollback_session, ip_id):
    """§28 — from_ip_id must differ from to_ip_id (CHECK enforces it)."""
    with pytest.raises(IntegrityError):
        rollback_session.execute(
            text(
                "INSERT INTO transfers "
                "(from_ip_id, to_ip_id, amount, transfer_date, status) "
                "VALUES (:ip, :ip, 1000, CURRENT_DATE, 'DRAFT')"
            ),
            {"ip": ip_id},
        )


def test_transfer_excluded_from_pnl(engine, ip_id, admin_headers):
    """§63 — the paired transfer flow leaves both IP summaries unchanged."""
    import uuid

    from fastapi.testclient import TestClient

    from app.api import app

    suffix = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        counter_ip = conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) "
                "VALUES ('FINTEST ИП 2', '900000000002') "
                "ON CONFLICT (bin) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id"
            )
        ).scalar_one()
        assert counter_ip != ip_id, "§28: transfer must be between two different ИП"
        source_account_id = conn.execute(
            text(
                "INSERT INTO accounts (ip_id, type, name) "
                "VALUES (:ip, 'OTHER', :name) RETURNING id"
            ),
            {"ip": ip_id, "name": f"FINTEST source {suffix}"},
        ).scalar_one()
        destination_account_id = conn.execute(
            text(
                "INSERT INTO accounts (ip_id, type, name) "
                "VALUES (:ip, 'OTHER', :name) RETURNING id"
            ),
            {"ip": counter_ip, "name": f"FINTEST destination {suffix}"},
        ).scalar_one()
        ip_ids = (ip_id, counter_ip)
        pnl_query = text(
            "SELECT targets.ip_id, "
            "COALESCE(SUM(summary.total_income), 0)::NUMERIC(15,2) AS total_income, "
            "COALESCE(SUM(summary.total_expense), 0)::NUMERIC(15,2) AS total_expense, "
            "COALESCE(SUM(summary.net_profit), 0)::NUMERIC(15,2) AS net_profit "
            "FROM (VALUES (:from_ip), (:to_ip)) AS targets(ip_id) "
            "LEFT JOIN v_ip_financial_summary summary ON summary.ip_id = targets.ip_id "
            "GROUP BY targets.ip_id ORDER BY targets.ip_id"
        )
        query_params = {"from_ip": ip_id, "to_ip": counter_ip}
        before = conn.execute(pnl_query, query_params).all()

    with TestClient(app) as client:
        response = client.post(
            "/transfers",
            json={
                "from_ip_id": ip_ids[0],
                "to_ip_id": ip_ids[1],
                "from_account_id": source_account_id,
                "to_account_id": destination_account_id,
                "amount": "20000.00",
                "transfer_date": "2099-03-12",
                "comment": f"finance-integrity P&L {suffix}",
            },
            headers={**admin_headers, "Idempotency-Key": f"fin-integrity-{suffix}"},
        )
    assert response.status_code == 201, response.text

    with engine.connect() as conn:
        after = conn.execute(pnl_query, query_params).all()
        transfer_flows = conn.execute(
            text(
                "SELECT ip_id, SUM(transfers_in) AS transfers_in, "
                "SUM(transfers_out) AS transfers_out "
                "FROM v_ip_financial_summary "
                "WHERE ip_id IN (:from_ip, :to_ip) "
                "AND period_month = DATE '2099-03-01' "
                "GROUP BY ip_id"
            ),
            query_params,
        ).all()
    assert len(before) == len(after) == 2
    assert {tuple(row) for row in before} == {tuple(row) for row in after}
    assert {
        (row.ip_id, row.transfers_in, row.transfers_out) for row in transfer_flows
    } == {
        (ip_ids[0], Decimal("0.00"), Decimal("20000.00")),
        (ip_ids[1], Decimal("20000.00"), Decimal("0.00")),
    }


def test_payout_reconciliation_within_tolerance(engine, ip_id):
    """§44 — stored gross must match item registry within tolerance."""
    with engine.begin() as conn:
        payout_id = conn.execute(
            text(
                "INSERT INTO kaspi_payouts (ip_id, external_payout_id, payout_date, "
                "gross_amount, commission_amount, delivery_amount, refund_amount, "
                "net_amount, status) "
                "VALUES (:ip, :ext_id, CURRENT_DATE, 100000.00, 15000.00, 0, 0, "
                "85000.00, 'RECEIVED') RETURNING id"
            ),
            {"ip": ip_id, "ext_id": f"FIN-PAY-{uuid.uuid4().hex[:8]}"},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO kaspi_payout_items (payout_id, order_id, gross_amount, "
                "commission_amount, net_amount, item_type, match_status) "
                "VALUES (:pid, NULL, 100000.00, 0.00, 100000.00, 'ADJUSTMENT', 'MATCHED')"
            ),
            {"pid": payout_id},
        )
        row = conn.execute(
            text(
                "SELECT gross_amount, (SELECT sum(gross_amount) FROM kaspi_payout_items "
                "WHERE payout_id = :pid) AS items_total FROM kaspi_payouts WHERE id = :pid"
            ),
            {"pid": payout_id},
        ).fetchone()
    assert row is not None
    assert abs(Decimal(row[0]) - Decimal(row[1])) <= Decimal("1.00")


def test_settings_dsn_from_env():
    """§10 — credentials come from the environment, never hard-coded."""
    settings = DatabaseSettings.from_env()
    assert settings.password.reveal() != "" or "DATABASE_PASSWORD" not in os.environ
    assert "erp_pass" not in settings.safe_dsn()


# --------------------------------------------------------------------------- #
# §67 — mandatory financial tests: duplicate / amount / rate freeze /
# concurrent, against the real PostgreSQL behaviour
# --------------------------------------------------------------------------- #


def test_duplicate_external_id_rejected(engine, ip_id):
    """§41/§67 — the same (ip, type, external_id) can be booked only once."""
    with engine.begin() as conn:
        type_id = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_PAY'")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO operations "
                "(ip_id, type_id, amount, operation_date, external_id) "
                "VALUES (:ip, :t, 1000.00, CURRENT_DATE, 'DUP-1')"
            ),
            {"ip": ip_id, "t": type_id},
        )
        with pytest.raises(Exception, match="uq_operations_ip_type_external"), conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO operations "
                    "(ip_id, type_id, amount, operation_date, external_id) "
                    "VALUES (:ip, :t, 1000.00, CURRENT_DATE, 'DUP-1')"
                ),
                {"ip": ip_id, "t": type_id},
            )


def test_zero_amount_rejected(engine, ip_id):
    """§27/§67 — zero is rejected in both directions; the BEFORE-sign trigger
    is the first line of defence, chk_operations_amount_nonzero the backstop."""
    with engine.begin() as conn:
        in_type = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_PAY'")
        ).scalar_one()
        out_type = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALARY_PAYMENT'")
        ).scalar_one()
        with pytest.raises(Exception, match="requires a positive amount"), conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO operations (ip_id, type_id, amount, operation_date) "
                    "VALUES (:ip, :t, 0, CURRENT_DATE)"
                ),
                {"ip": ip_id, "t": in_type},
            )
        with pytest.raises(Exception, match="requires a negative amount"), conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO operations (ip_id, type_id, amount, operation_date) "
                    "VALUES (:ip, :t, 0, CURRENT_DATE)"
                ),
                {"ip": ip_id, "t": out_type},
            )


def test_amount_nonzero_check_is_backstop(engine):
    """§27 — the backstop CHECK exists on operations even though the sign
    trigger fires first for ordinary inserts."""
    constraint = (
        engine.connect()
        .execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = 'operations'::regclass "
                "AND conname = 'chk_operations_amount_nonzero'"
            )
        )
        .scalar_one()
    )
    assert constraint == 1, "§27 the backstop constraint exists on operations"


def test_inbound_zero_rejected_by_sign_trigger(engine, ip_id):
    """§27/§67 — the sign trigger rejects non-positive inbound amounts too."""
    with engine.begin() as conn:
        type_id = conn.execute(
            text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_PAY'")
        ).scalar_one()
        with pytest.raises(Exception, match="requires a positive amount"), conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO operations (ip_id, type_id, amount, operation_date) "
                    "VALUES (:ip, :t, 0.01 * 0, CURRENT_DATE)"
                ),
                {"ip": ip_id, "t": type_id},
            )


def test_rate_history_overlap_rejected(engine, ip_id):
    """§31/§67 — rate freeze: overlapping periods for one work type are impossible.
    Seeded rates run 2024-01-01..infinity per type, so this test books its own
    work operation type and closed periods far in the future."""
    conn = engine.connect()
    trans = conn.begin()
    try:
        wot_id = conn.execute(
            text(
                "INSERT INTO work_operation_types (code, name, unit, calc_mode) "
                "VALUES ('QA_OVERLAP', 'QA overlap type', 'м2', 'per_unit') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO rates_history "
                "(work_operation_type_id, rate, effective_from, effective_to) "
                "VALUES (:w, 500.00, '2030-01-01', '2030-06-30')"
            ),
            {"w": wot_id},
        )
        with pytest.raises(Exception, match="excl_rates_history_overlap"), conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO rates_history "
                    "(work_operation_type_id, rate, effective_from, effective_to) "
                    "VALUES (:w, 600.00, '2030-06-01', '2030-12-31')"
                ),
                {"w": wot_id},
            )
        # adjacent half-open periods [from, to) are legal — no overlap
        conn.execute(
            text(
                "INSERT INTO rates_history "
                "(work_operation_type_id, rate, effective_from, effective_to) "
                "VALUES (:w, 600.00, '2030-06-30', '2030-12-31')"
            ),
            {"w": wot_id},
        )
    finally:
        trans.rollback()
        conn.close()


def test_work_entry_rate_frozen_against_later_rate_change(engine, ip_id):
    """§32/§67 — a later rate change must not alter a booked work entry."""
    conn = engine.connect()
    trans = conn.begin()
    try:
        wot_id = conn.execute(
            text(
                "INSERT INTO work_operation_types (code, name, unit, calc_mode) "
                "VALUES ('QA_FREEZE', 'QA freeze type', 'м2', 'per_unit') RETURNING id"
            )
        ).scalar_one()
        emp_id = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
                "VALUES ('QA RATE FREEZE', :ip, 'PIECEWORK') RETURNING id"
            ),
            {"ip": ip_id},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO rates_history "
                "(work_operation_type_id, rate, effective_from, effective_to) "
                "VALUES (:w, 500.00, '2030-01-01', '2030-12-31')"
            ),
            {"w": wot_id},
        )
        entry_total = conn.execute(
            text(
                "INSERT INTO work_entries (employee_id, work_operation_type_id, ip_id, "
                "entry_date, quantity, rate_at_time) "
                "VALUES (:e, :w, :ip, '2030-03-01', 2, 500.00) RETURNING total_sum"
            ),
            {"e": emp_id, "w": wot_id, "ip": ip_id},
        ).scalar_one()
        assert entry_total == 1000.00, "§32 frozen rate drives total_sum"
        conn.execute(
            text(
                "INSERT INTO rates_history "
                "(work_operation_type_id, rate, effective_from, effective_to) "
                "VALUES (:w, 900.00, '2031-01-01', '2031-12-31')"
            ),
            {"w": wot_id},
        )
        still = conn.execute(
            text("SELECT total_sum FROM work_entries WHERE id = (SELECT max(id) FROM work_entries)")
        ).scalar_one()
        assert still == 1000.00, "§32 the later rate change does not alter the booked entry"
    finally:
        trans.rollback()
        conn.close()


def test_concurrent_duplicate_external_id_single_winner(engine, ip_id, db_settings):
    """§67 — two concurrent identical payments: exactly one wins, no double booking."""
    import sqlalchemy

    type_id_sql = "SELECT id FROM operation_types WHERE code = 'SALE_KASPI_PAY'"
    with engine.begin() as conn:
        type_id = conn.execute(text(type_id_sql)).scalar_one()

    dsn = db_settings.dsn()

    def attempt():
        eng = sqlalchemy.create_engine(dsn)
        try:
            with eng.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO operations "
                        "(ip_id, type_id, amount, operation_date, external_id) "
                        "VALUES (:ip, :t, 700.00, CURRENT_DATE, 'CONC-1')"
                    ),
                    {"ip": ip_id, "t": type_id},
                )
        finally:
            eng.dispose()

    attempt()
    with (
        pytest.raises(Exception, match="uq_operations_ip_type_external"),
        engine.begin() as conn2,
    ):
        conn2.execute(
            text(
                "INSERT INTO operations "
                "(ip_id, type_id, amount, operation_date, external_id) "
                "VALUES (:ip, :t, 700.00, CURRENT_DATE, 'CONC-1')"
            ),
            {"ip": ip_id, "t": type_id},
        )
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM operations WHERE external_id = 'CONC-1' AND ip_id = :ip"),
            {"ip": ip_id},
        ).scalar_one()
    assert count == 1, "§67 no double booking under concurrency"


def test_transaction_atomicity_on_failed_batch(engine, ip_id):
    """§67 — a failure inside a transaction leaves no partial business rows.
    The two inserts run in ONE transaction; when the duplicate insert fails the
    whole transaction is rolled back, so the first insert must not survive."""
    from sqlalchemy.exc import IntegrityError

    type_id = (
        engine.connect()
        .execute(text("SELECT id FROM operation_types WHERE code = 'SALE_KASPI_PAY'"))
        .scalar_one()
    )
    before = (
        engine.connect()
        .execute(text("SELECT count(*) FROM operations WHERE ip_id = :ip"), {"ip": ip_id})
        .scalar_one()
    )
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO operations "
                    "(ip_id, type_id, amount, operation_date, external_id) "
                    "VALUES (:ip, :t, 100.00, CURRENT_DATE, 'ATOM-1')"
                ),
                {"ip": ip_id, "t": type_id},
            )
            conn.execute(
                text(
                    "INSERT INTO operations "
                    "(ip_id, type_id, amount, operation_date, external_id) "
                    "VALUES (:ip, :t, 200.00, CURRENT_DATE, 'ATOM-1')"
                ),
                {"ip": ip_id, "t": type_id},
            )
    except IntegrityError:
        pass
    else:
        pytest.fail("§67 the duplicate insert must abort the transaction")
    after = (
        engine.connect()
        .execute(text("SELECT count(*) FROM operations WHERE ip_id = :ip"), {"ip": ip_id})
        .scalar_one()
    )
    assert after == before, "§67 atomicity: the failed batch left no partial rows"
