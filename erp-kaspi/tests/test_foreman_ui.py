"""§61 — API tests for the foreman work-entry endpoint (auto rate freeze)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.api import app


def _seed_foreman_data(engine: Engine) -> dict[str, int]:
    """ИП + employee + work type + rate; ids are unique per call (TD-012)."""
    run = uuid.uuid4().hex[:10]  # per-call suffix: two tests never share one work type
    with engine.begin() as conn:
        ip_id = conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) VALUES (:n, :b) "
                "ON CONFLICT (bin) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            ),
            {"n": f"FOREMAN ИП {run}", "b": f"8{str(int(run, 16) % 10**11).zfill(11)}"},
        ).scalar_one()
        emp_id = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
                "VALUES (:n, :ip, 'PIECEWORK') RETURNING id"
            ),
            {"n": f"Бригадир {run}", "ip": ip_id},
        ).scalar_one()
        wot_id = conn.execute(
            text(
                "INSERT INTO work_operation_types (code, name, unit, calc_mode) "
                "VALUES (:c, :n, 'pcs', 'per_unit') ON CONFLICT (code) DO NOTHING RETURNING id"
            ),
            {"c": f"FOREMAN_{run}", "n": f"Операция {run}"},
        ).scalar_one_or_none()
        if wot_id is None:
            wot_id = conn.execute(
                text("SELECT id FROM work_operation_types WHERE code = :c"),
                {"c": f"FOREMAN_{run}"},
            ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO rates_history "
                "(work_operation_type_id, rate, effective_from, effective_to) "
                "VALUES (:w, 250.00, TIMESTAMPTZ '2026-01-01', "
                "TIMESTAMPTZ '2026-06-01')"
            ),
            {"w": wot_id},
        )
    return {"ip_id": ip_id, "employee_id": emp_id, "work_type_id": wot_id}


def test_foreman_work_entry_freezes_rate_and_total(engine, applied_schema, admin_headers):
    """§61/§32 — POST /payroll/work-entries freezes the rate automatically."""
    ids = _seed_foreman_data(engine)
    with TestClient(app) as client:
        body = client.post(
            "/payroll/work-entries",
            headers=admin_headers,
            json={
                "employee_id": ids["employee_id"],
                "work_operation_type_id": ids["work_type_id"],
                "ip_id": ids["ip_id"],
                "entry_date": "2026-03-15",
                "quantity": 4,
            },
        )
        assert body.status_code == 201, body.text
        payload = body.json()
        entry_id = payload["id"]

        row = (
            engine.connect()
            .execute(
                text("SELECT rate_at_time, total_sum, status FROM work_entries WHERE id = :i"),
                {"i": entry_id},
            )
            .one()
        )
    assert float(row.rate_at_time) == 250.00, "§32: the rate in force is frozen"
    assert float(row.total_sum) == 1000.00, "§32: total = quantity * rate, once"
    assert row.status == "DRAFT"


def test_foreman_work_entry_order_link(engine, applied_schema, admin_headers):
    """§61 — the foreman form may bind an order; the link is stored verbatim."""
    ids = _seed_foreman_data(engine)
    with engine.begin() as conn:
        order_id = conn.execute(
            text(
                "INSERT INTO orders (ip_id, external_order_id, order_date, status) "
                "VALUES (:ip, :ext, :d, 'NEW') RETURNING id"
            ),
            {"ip": ids["ip_id"], "ext": f"ORD-{uuid.uuid4().hex[:10]}", "d": "2026-03-14"},
        ).scalar_one()
    with TestClient(app) as client:
        body = client.post(
            "/payroll/work-entries",
            headers=admin_headers,
            json={
                "employee_id": ids["employee_id"],
                "work_operation_type_id": ids["work_type_id"],
                "ip_id": ids["ip_id"],
                "order_id": order_id,
                "entry_date": "2026-03-15",
                "quantity": 2,
            },
        )
        assert body.status_code == 201, body.text
    stored = (
        engine.connect()
        .execute(
            text("SELECT order_id FROM work_entries WHERE id = :i"),
            {"i": body.json()["id"]},
        )
        .scalar_one()
    )
    assert stored == order_id


def test_foreman_work_entry_without_rate_is_rejected(engine, applied_schema, admin_headers):
    """§61 — a missing rate is a hard error, never a silent zero (§32)."""
    ids = _seed_foreman_data(engine)
    with TestClient(app) as client:
        body = client.post(
            "/payroll/work-entries",
            headers=admin_headers,
            json={
                "employee_id": ids["employee_id"],
                "work_operation_type_id": ids["work_type_id"],
                "ip_id": ids["ip_id"],
                "entry_date": "2025-01-01",  # before any rate window
                "quantity": 1,
            },
        )
    assert body.status_code == 422, body.text  # §94 domain errors map to 422


def test_work_entry_idempotency_key(engine, applied_schema, admin_headers):
    """C-2: POST /payroll/work-entries with Idempotency-Key header is idempotent.
    A retried request with the same key returns the existing entry."""
    ids = _seed_foreman_data(engine)
    key = f"c2-test-{uuid.uuid4().hex}"
    with TestClient(app) as client:
        first = client.post(
            "/payroll/work-entries",
            headers={**admin_headers, "Idempotency-Key": key},
            json={
                "employee_id": ids["employee_id"],
                "work_operation_type_id": ids["work_type_id"],
                "ip_id": ids["ip_id"],
                "entry_date": "2026-03-15",
                "quantity": 4,
            },
        )
        assert first.status_code == 201, first.text
        second = client.post(
            "/payroll/work-entries",
            headers={**admin_headers, "Idempotency-Key": key},
            json={
                "employee_id": ids["employee_id"],
                "work_operation_type_id": ids["work_type_id"],
                "ip_id": ids["ip_id"],
                "entry_date": "2026-03-15",
                "quantity": 4,
            },
        )
    assert second.status_code == 201, second.text
    payload = second.json()
    assert payload["id"] == first.json()["id"], "replay must return the same entry"
    assert payload["status"] == "IDEMPOTENT_REPLAY"
