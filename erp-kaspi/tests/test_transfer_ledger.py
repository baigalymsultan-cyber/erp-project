from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.api import app

TRANSFER_DATE = date(2099, 3, 12)


def _create_ip_pair(engine: Engine) -> tuple[int, int]:
    suffix = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        ids = []
        for number in (1, 2):
            bin_value = f"{(int(suffix, 16) + number) % 10**12:012d}"
            ids.append(
                conn.execute(
                    text(
                        "INSERT INTO ip_entities (name, bin) VALUES (:name, :bin) "
                        "RETURNING id"
                    ),
                    {"name": f"Transfer {suffix} {number}", "bin": bin_value},
                ).scalar_one()
            )
    return int(ids[0]), int(ids[1])


def _create_account_pair(engine: Engine, from_ip: int, to_ip: int) -> tuple[int, int]:
    suffix = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        account_ids = []
        for ip_id, direction in ((from_ip, "from"), (to_ip, "to")):
            account_ids.append(
                conn.execute(
                    text(
                        "INSERT INTO accounts (ip_id, type, name) "
                        "VALUES (:ip_id, 'OTHER', :name) RETURNING id"
                    ),
                    {"ip_id": ip_id, "name": f"Transfer {direction} {suffix}"},
                ).scalar_one()
            )
    return int(account_ids[0]), int(account_ids[1])


def _create_user(engine: Engine, ip_id: int, role_code: str) -> tuple[str, str, int]:
    from app.core.security import hash_password

    suffix = uuid.uuid4().hex[:12]
    username = f"transfer-{role_code.lower()}-{suffix}"
    password = f"Transfer-Test-{suffix}!"
    with engine.begin() as conn:
        employee_id = conn.execute(
            text(
                "INSERT INTO employees (full_name, assigned_ip_id, payment_type) "
                "VALUES (:name, :ip_id, 'PIECEWORK') RETURNING id"
            ),
            {"name": f"Transfer {role_code} {suffix}", "ip_id": ip_id},
        ).scalar_one()
        user_id = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, employee_id, is_active) "
                "VALUES (:username, :password_hash, :employee_id, TRUE) RETURNING id"
            ),
            {
                "username": username,
                "password_hash": hash_password(password),
                "employee_id": employee_id,
            },
        ).scalar_one()
        assigned = conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT :user_id, id FROM roles WHERE code = :role_code"
            ),
            {"user_id": user_id, "role_code": role_code},
        )
        assert assigned.rowcount == 1, f"role {role_code} is not seeded"
    return username, password, int(user_id)


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _transfer_body(
    from_ip: int,
    to_ip: int,
    from_account_id: int,
    to_account_id: int,
    amount: str = "77.35",
) -> dict[str, object]:
    return {
        "from_ip_id": from_ip,
        "to_ip_id": to_ip,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "amount": amount,
        "transfer_date": TRANSFER_DATE.isoformat(),
        "comment": "cross-IP transfer test",
    }


def _post_transfer(
    client: TestClient,
    headers: dict[str, str],
    from_ip: int,
    to_ip: int,
    from_account_id: int,
    to_account_id: int,
    key: str,
    amount: str = "77.35",
):
    return client.post(
        "/transfers",
        json=_transfer_body(from_ip, to_ip, from_account_id, to_account_id, amount),
        headers={**headers, "Idempotency-Key": key},
    )



def _assert_integrity_error_without_persisting(
    engine: Engine,
    query: str,
    params: dict[str, object],
) -> None:
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            with pytest.raises(IntegrityError):
                conn.execute(text(query), params)
        finally:
            transaction.rollback()


@pytest.fixture
def migrator_engine() -> Iterator[Engine]:
    from app.core.config import DatabaseSettings
    from app.core.database import create_database_engine

    env = dict(os.environ)
    password = env.get("TEST_DATABASE_MIGRATOR_PASSWORD") or env.get("DB_MIGRATOR_PASSWORD")
    if not password:
        pytest.skip("Transfer failure injection requires a test migrator password")
    env["DATABASE_USER"] = env.get("TEST_DATABASE_MIGRATOR_USER") or env.get(
        "DB_MIGRATOR_USER", "erp_migrator"
    )
    env["DATABASE_PASSWORD"] = password
    privileged_engine = create_database_engine(DatabaseSettings.from_env(env))
    try:
        yield privileged_engine
    finally:
        privileged_engine.dispose()


@pytest.mark.parametrize("role_code", ["ADMIN", "DIRECTOR"])
def test_transfer_creates_linked_neutral_operation_legs(
    engine: Engine, applied_schema, role_code: str
):
    from_ip, to_ip = _create_ip_pair(engine)
    from_account_id, to_account_id = _create_account_pair(engine, from_ip, to_ip)
    idempotency_key = f"transfer-{uuid.uuid4().hex}"
    username, password, actor_id = _create_user(engine, from_ip, role_code)
    body = _transfer_body(from_ip, to_ip, from_account_id, to_account_id)
    params = {
        "period_start": TRANSFER_DATE.isoformat(),
        "period_end": TRANSFER_DATE.isoformat(),
    }

    with TestClient(app) as client:
        headers = _login(client, username, password)
        before = client.get("/pnl", params=params, headers=headers)
        response = _post_transfer(
            client,
            headers,
            from_ip,
            to_ip,
            from_account_id,
            to_account_id,
            idempotency_key,
        )
        after = client.get("/pnl", params=params, headers=headers)

    assert response.status_code == 201, response.text
    assert before.status_code == 200, before.text
    assert after.status_code == 200, after.text
    assert after.json() == before.json()

    with engine.connect() as conn:
        transfer = conn.execute(
            text(
                "SELECT from_ip_id, to_ip_id, from_account_id, to_account_id, amount, "
                "reason, status, created_by, outbound_operation_id, inbound_operation_id "
                "FROM transfers WHERE id = :transfer_id"
            ),
            {"transfer_id": response.json()["id"]},
        ).mappings().one()
        legs = conn.execute(
            text(
                "SELECT o.id, o.ip_id, o.amount, o.status, o.account_id, o.created_by, "
                "o.comment, o.related_operation_id, ot.code, ot.kind, ot.affects_pnl "
                "FROM operations o JOIN operation_types ot ON ot.id = o.type_id "
                "WHERE o.id IN (:out_id, :in_id)"
            ),
            {
                "out_id": transfer["outbound_operation_id"],
                "in_id": transfer["inbound_operation_id"],
            },
        ).mappings().all()
        audit_count = conn.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE user_id = :user_id "
                "AND action = 'INSERT' AND ((entity = 'transfers' AND entity_id = :transfer_id) "
                "OR (entity = 'operations' AND entity_id IN (:out_id, :in_id)))"
            ),
            {
                "user_id": actor_id,
                "transfer_id": response.json()["id"],
                "out_id": transfer["outbound_operation_id"],
                "in_id": transfer["inbound_operation_id"],
            },
        ).scalar_one()

    assert transfer["outbound_operation_id"] is not None
    assert transfer["inbound_operation_id"] is not None
    assert transfer["from_ip_id"] == from_ip
    assert transfer["to_ip_id"] == to_ip
    assert transfer["from_account_id"] == from_account_id
    assert transfer["to_account_id"] == to_account_id
    assert transfer["amount"] == Decimal("77.35")
    assert transfer["reason"] == body["comment"]
    assert transfer["status"] == "POSTED"
    assert transfer["created_by"] == actor_id
    assert len(legs) == 2
    by_code = {row["code"]: row for row in legs}
    outbound = by_code["TRANSFER_OUT"]
    inbound = by_code["TRANSFER_IN"]
    assert outbound["ip_id"] == from_ip
    assert inbound["ip_id"] == to_ip
    assert outbound["account_id"] == from_account_id
    assert inbound["account_id"] == to_account_id
    assert outbound["amount"] == Decimal("-77.35")
    assert inbound["amount"] == Decimal("77.35")
    assert outbound["amount"] + inbound["amount"] == Decimal("0.00")
    assert outbound["status"] == inbound["status"] == "POSTED"
    assert outbound["kind"] == inbound["kind"] == "TRANSFER"
    assert not outbound["affects_pnl"] and not inbound["affects_pnl"]
    assert outbound["created_by"] == actor_id and inbound["created_by"] == actor_id
    assert outbound["comment"] == inbound["comment"] == body["comment"]
    assert outbound["related_operation_id"] == inbound["id"]
    assert inbound["related_operation_id"] == outbound["id"]
    assert audit_count == 3


def test_posted_transfer_requires_complete_ledger_pair(engine: Engine, applied_schema):
    from_ip, to_ip = _create_ip_pair(engine)
    _assert_integrity_error_without_persisting(
        engine,
        "INSERT INTO transfers (from_ip_id, to_ip_id, amount, transfer_date) "
        "VALUES (:from_ip, :to_ip, 1.00, :transfer_date)",
        {
            "from_ip": from_ip,
            "to_ip": to_ip,
            "transfer_date": TRANSFER_DATE,
        },
    )


@pytest.mark.parametrize("corruption", ["amount", "ip", "type", "pair"])
def test_database_rejects_malformed_posted_transfer_pair(
    engine: Engine, applied_schema, corruption: str
):
    from_ip, to_ip = _create_ip_pair(engine)
    from_account_id, to_account_id = _create_account_pair(engine, from_ip, to_ip)
    _username, _password, actor_id = _create_user(engine, from_ip, "ADMIN")
    transfer_amount = Decimal("77.35")
    outbound_amount = (
        transfer_amount
        if corruption == "type"
        else Decimal("-77.34")
        if corruption == "amount"
        else -transfer_amount
    )
    outbound_ip = to_ip if corruption == "ip" else from_ip
    outbound_account = to_account_id if corruption == "ip" else from_account_id
    detected = False

    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            type_ids = dict(
                conn.execute(
                    text(
                        "SELECT code, id FROM operation_types "
                        "WHERE code IN ('TRANSFER_OUT', 'TRANSFER_IN', 'SALE_KASPI_PAY')"
                    )
                ).all()
            )
            outbound_type = type_ids[
                "SALE_KASPI_PAY" if corruption == "type" else "TRANSFER_OUT"
            ]
            outbound_id = conn.execute(
                text(
                    "INSERT INTO operations "
                    "(ip_id, type_id, account_id, amount, operation_date, comment, "
                    "status, created_by) "
                    "VALUES (:ip_id, :type_id, :account_id, :amount, :op_date, "
                    ":comment, 'DRAFT', :actor_id) RETURNING id"
                ),
                {
                    "ip_id": outbound_ip,
                    "type_id": outbound_type,
                    "account_id": outbound_account,
                    "amount": outbound_amount,
                    "op_date": TRANSFER_DATE,
                    "comment": "invalid direct transfer write",
                    "actor_id": actor_id,
                },
            ).scalar_one()
            inbound_id = conn.execute(
                text(
                    "INSERT INTO operations "
                    "(ip_id, type_id, account_id, amount, operation_date, comment, "
                    "status, created_by) "
                    "VALUES (:ip_id, :type_id, :account_id, :amount, :op_date, "
                    ":comment, 'DRAFT', :actor_id) RETURNING id"
                ),
                {
                    "ip_id": to_ip,
                    "type_id": type_ids["TRANSFER_IN"],
                    "account_id": to_account_id,
                    "amount": transfer_amount,
                    "op_date": TRANSFER_DATE,
                    "comment": "invalid direct transfer write",
                    "actor_id": actor_id,
                },
            ).scalar_one()
            conn.execute(
                text(
                    "UPDATE operations SET related_operation_id = :related_id "
                    "WHERE id = :operation_id"
                ),
                {
                    "operation_id": outbound_id,
                    "related_id": None if corruption == "pair" else inbound_id,
                },
            )
            conn.execute(
                text(
                    "UPDATE operations SET related_operation_id = :related_id "
                    "WHERE id = :operation_id"
                ),
                {"operation_id": inbound_id, "related_id": outbound_id},
            )
            conn.execute(
                text(
                    "INSERT INTO transfers "
                    "(from_ip_id, to_ip_id, from_account_id, to_account_id, amount, "
                    "transfer_date, reason, outbound_operation_id, inbound_operation_id, "
                    "idempotency_key, status, created_by) "
                    "VALUES (:from_ip, :to_ip, :from_account, :to_account, :amount, "
                    ":transfer_date, :reason, :outbound_id, :inbound_id, :key, 'DRAFT', :actor_id)"
                ),
                {
                    "from_ip": from_ip,
                    "to_ip": to_ip,
                    "from_account": from_account_id,
                    "to_account": to_account_id,
                    "amount": transfer_amount,
                    "transfer_date": TRANSFER_DATE,
                    "reason": "invalid direct transfer write",
                    "outbound_id": outbound_id,
                    "inbound_id": inbound_id,
                    "key": f"invalid-db-{uuid.uuid4().hex}",
                    "actor_id": actor_id,
                },
            )
            conn.execute(
                text("UPDATE operations SET status = 'POSTED' WHERE id IN (:out_id, :in_id)"),
                {"out_id": outbound_id, "in_id": inbound_id},
            )
            conn.execute(
                text(
                    "UPDATE transfers SET status = 'POSTED' "
                    "WHERE outbound_operation_id = :out_id"
                ),
                {"out_id": outbound_id},
            )
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except IntegrityError:
                detected = True
        finally:
            transaction.rollback()

    assert detected, f"database accepted malformed posted transfer pair: {corruption}"


def test_posted_transfer_leg_must_belong_to_one_transfer(
    engine: Engine, applied_schema
):
    from_ip, to_ip = _create_ip_pair(engine)
    from_account_id, _to_account_id = _create_account_pair(engine, from_ip, to_ip)
    _username, _password, actor_id = _create_user(engine, from_ip, "ADMIN")
    detected = False

    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO operations "
                    "(ip_id, type_id, account_id, amount, operation_date, comment, "
                    "status, created_by) "
                    "VALUES (:ip_id, (SELECT id FROM operation_types WHERE code = 'TRANSFER_OUT'), "
                    ":account_id, -10.00, :op_date, 'unlinked transfer leg', 'POSTED', :actor_id)"
                ),
                {
                    "ip_id": from_ip,
                    "account_id": from_account_id,
                    "op_date": TRANSFER_DATE,
                    "actor_id": actor_id,
                },
            )
            try:
                conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            except IntegrityError:
                detected = True
        finally:
            transaction.rollback()

    assert detected, "database accepted a posted transfer leg without a transfer"


def test_transfer_posted_legs_constraint_is_validated(engine: Engine, applied_schema):
    with engine.connect() as conn:
        constraint = conn.execute(
            text(
                "SELECT convalidated, pg_get_constraintdef(oid) AS definition "
                "FROM pg_constraint "
                "WHERE conrelid = 'public.transfers'::regclass "
                "AND conname = 'chk_transfers_posted_has_legs'"
            )
        ).one_or_none()
    assert constraint is not None
    assert constraint.convalidated is True
    assert "outbound_operation_id IS NOT NULL" in constraint.definition
    assert "inbound_operation_id IS NOT NULL" in constraint.definition


def test_transfer_database_rejects_account_for_wrong_ip(engine: Engine, applied_schema):
    from_ip, to_ip = _create_ip_pair(engine)
    wrong_source_account_id, destination_account_id = _create_account_pair(
        engine, to_ip, to_ip
    )
    _assert_integrity_error_without_persisting(
        engine,
        "INSERT INTO transfers "
        "(from_ip_id, to_ip_id, from_account_id, to_account_id, amount, "
        "transfer_date, status) "
        "VALUES (:from_ip, :to_ip, :from_account, :to_account, 10.00, :transfer_date, 'DRAFT')",
        {
            "from_ip": from_ip,
            "to_ip": to_ip,
            "from_account": wrong_source_account_id,
            "to_account": destination_account_id,
            "transfer_date": TRANSFER_DATE,
        },
    )


def test_transfer_requires_idempotency_key(engine: Engine, applied_schema, admin_headers):
    from_ip, to_ip = _create_ip_pair(engine)
    from_account_id, to_account_id = _create_account_pair(engine, from_ip, to_ip)
    with TestClient(app) as client:
        response = client.post(
            "/transfers",
            json=_transfer_body(from_ip, to_ip, from_account_id, to_account_id),
            headers=admin_headers,
        )
    assert response.status_code == 422, response.text
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM transfers WHERE from_ip_id = :ip_id"),
            {"ip_id": from_ip},
        ).scalar_one()
    assert count == 0


def test_transfer_requires_authentication(engine: Engine, applied_schema):
    from_ip, to_ip = _create_ip_pair(engine)
    from_account_id, to_account_id = _create_account_pair(engine, from_ip, to_ip)
    key = f"transfer-{uuid.uuid4().hex}"
    with TestClient(app) as client:
        response = client.post(
            "/transfers",
            json=_transfer_body(from_ip, to_ip, from_account_id, to_account_id),
            headers={"Idempotency-Key": key},
        )
    assert response.status_code == 401, response.text
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM transfers WHERE from_ip_id = :ip_id"),
            {"ip_id": from_ip},
        ).scalar_one()
    assert count == 0


def test_same_ip_account_transfer_is_rejected(engine: Engine, applied_schema, admin_headers):
    from_ip, _ = _create_ip_pair(engine)
    from_account_id, to_account_id = _create_account_pair(engine, from_ip, from_ip)
    key = f"transfer-{uuid.uuid4().hex}"
    with TestClient(app) as client:
        response = _post_transfer(
            client,
            admin_headers,
            from_ip,
            from_ip,
            from_account_id,
            to_account_id,
            key,
        )
    assert response.status_code == 422, response.text
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM transfers WHERE from_ip_id = :ip_id"),
            {"ip_id": from_ip},
        ).scalar_one()
    assert count == 0


def test_transfer_rejects_account_owned_by_another_ip(
    engine: Engine, applied_schema, admin_headers
):
    from_ip, to_ip = _create_ip_pair(engine)
    foreign_account_id, to_account_id = _create_account_pair(engine, to_ip, to_ip)
    key = f"transfer-{uuid.uuid4().hex}"
    with TestClient(app) as client:
        response = _post_transfer(
            client,
            admin_headers,
            from_ip,
            to_ip,
            foreign_account_id,
            to_account_id,
            key,
        )
    assert response.status_code == 422, response.text
    with engine.connect() as conn:
        transfer_count = conn.execute(
            text("SELECT count(*) FROM transfers WHERE from_ip_id = :ip_id"),
            {"ip_id": from_ip},
        ).scalar_one()
        operation_count = conn.execute(
            text("SELECT count(*) FROM operations WHERE ip_id IN (:from_ip, :to_ip)"),
            {"from_ip": from_ip, "to_ip": to_ip},
        ).scalar_one()
    assert transfer_count == 0
    assert operation_count == 0


@pytest.mark.parametrize("inactive_side", ["source", "destination"])
def test_transfer_rejects_inactive_source_or_destination_account(
    engine: Engine, applied_schema, admin_headers, inactive_side: str
):
    from_ip, to_ip = _create_ip_pair(engine)
    from_account_id, to_account_id = _create_account_pair(engine, from_ip, to_ip)
    inactive_account_id = from_account_id if inactive_side == "source" else to_account_id
    key = f"transfer-inactive-{inactive_side}-{uuid.uuid4().hex}"
    comment = f"transfer inactive {inactive_side} {uuid.uuid4().hex}"

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE accounts SET is_active = FALSE WHERE id = :account_id"),
            {"account_id": inactive_account_id},
        )

    with TestClient(app) as client:
        response = client.post(
            "/transfers",
            json={
                **_transfer_body(from_ip, to_ip, from_account_id, to_account_id),
                "comment": comment,
            },
            headers={**admin_headers, "Idempotency-Key": key},
        )

    assert response.status_code == 422, response.text
    assert f"{inactive_side} account must be active" in response.json()["detail"]
    with engine.connect() as conn:
        transfer_count = conn.execute(
            text("SELECT count(*) FROM transfers WHERE idempotency_key = :key"),
            {"key": key},
        ).scalar_one()
        operation_count = conn.execute(
            text("SELECT count(*) FROM operations WHERE comment = :comment"),
            {"comment": comment},
        ).scalar_one()
    assert transfer_count == 0
    assert operation_count == 0


def test_transfer_idempotency_replay_and_payload_conflict(
    engine: Engine, applied_schema, admin_headers
):
    from_ip, to_ip = _create_ip_pair(engine)
    from_account_id, to_account_id = _create_account_pair(engine, from_ip, to_ip)
    key = f"transfer-{uuid.uuid4().hex}"
    body = _transfer_body(from_ip, to_ip, from_account_id, to_account_id)
    with TestClient(app) as client:
        first = client.post(
            "/transfers", json=body, headers={**admin_headers, "Idempotency-Key": key}
        )
        replay = client.post(
            "/transfers", json=body, headers={**admin_headers, "Idempotency-Key": key}
        )
        conflict = client.post(
            "/transfers",
            json=_transfer_body(from_ip, to_ip, from_account_id, to_account_id, "78.35"),
            headers={**admin_headers, "Idempotency-Key": key},
        )

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert conflict.status_code == 409, conflict.text
    with engine.connect() as conn:
        transfer_count = conn.execute(
            text("SELECT count(*) FROM transfers WHERE idempotency_key = :key"),
            {"key": key},
        ).scalar_one()
        legs_count = conn.execute(
            text(
                "SELECT count(*) FROM operations WHERE id IN ("
                "SELECT outbound_operation_id FROM transfers WHERE idempotency_key = :key "
                "UNION ALL "
                "SELECT inbound_operation_id FROM transfers WHERE idempotency_key = :key)"
            ),
            {"key": key},
        ).scalar_one()
        audit_count = conn.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE entity = 'transfers' "
                "AND entity_id = :transfer_id AND action = 'INSERT'"
            ),
            {"transfer_id": first.json()["id"]},
        ).scalar_one()
    assert transfer_count == 1
    assert legs_count == 2
    assert audit_count == 1


def test_transfer_concurrent_replay_creates_only_one_pair(
    engine: Engine, applied_schema, admin_headers
):
    from_ip, to_ip = _create_ip_pair(engine)
    from_account_id, to_account_id = _create_account_pair(engine, from_ip, to_ip)
    key = f"transfer-{uuid.uuid4().hex}"
    body = _transfer_body(from_ip, to_ip, from_account_id, to_account_id)
    barrier = threading.Barrier(2)

    def send():
        with TestClient(app) as client:
            barrier.wait(timeout=15)
            return client.post(
                "/transfers",
                json=body,
                headers={**admin_headers, "Idempotency-Key": key},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: send(), range(2)))

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    with engine.connect() as conn:
        transfer_count = conn.execute(
            text("SELECT count(*) FROM transfers WHERE idempotency_key = :key"),
            {"key": key},
        ).scalar_one()
        legs_count = conn.execute(
            text(
                "SELECT count(*) FROM operations WHERE id IN ("
                "SELECT outbound_operation_id FROM transfers WHERE idempotency_key = :key "
                "UNION ALL "
                "SELECT inbound_operation_id FROM transfers WHERE idempotency_key = :key)"
            ),
            {"key": key},
        ).scalar_one()
    assert transfer_count == 1
    assert legs_count == 2


def test_transfer_denies_cross_ip_idor_even_on_replay(
    engine: Engine, applied_schema, admin_headers
):
    from_ip, to_ip = _create_ip_pair(engine)
    from_account_id, to_account_id = _create_account_pair(engine, from_ip, to_ip)
    key = f"transfer-{uuid.uuid4().hex}"
    username, password, _actor_id = _create_user(engine, from_ip, "ACCOUNTANT")
    with TestClient(app) as client:
        original = _post_transfer(
            client,
            admin_headers,
            from_ip,
            to_ip,
            from_account_id,
            to_account_id,
            key,
        )
        assert original.status_code == 201, original.text
        headers = _login(client, username, password)
        replay = _post_transfer(
            client,
            headers,
            from_ip,
            to_ip,
            from_account_id,
            to_account_id,
            key,
        )
    assert replay.status_code == 403, replay.text
    with engine.connect() as conn:
        transfer_count = conn.execute(
            text("SELECT count(*) FROM transfers WHERE from_ip_id = :ip_id"),
            {"ip_id": from_ip},
        ).scalar_one()
        operation_count = conn.execute(
            text("SELECT count(*) FROM operations WHERE ip_id IN (:from_ip, :to_ip)"),
            {"from_ip": from_ip, "to_ip": to_ip},
        ).scalar_one()
    assert transfer_count == 1
    assert operation_count == 2


def test_posted_transfer_legs_cannot_be_reversed_singly_or_mutated(
    engine: Engine, applied_schema, admin_headers
):
    from_ip, to_ip = _create_ip_pair(engine)
    from_account_id, to_account_id = _create_account_pair(engine, from_ip, to_ip)
    key = f"transfer-{uuid.uuid4().hex}"
    with TestClient(app) as client:
        created = _post_transfer(
            client,
            admin_headers,
            from_ip,
            to_ip,
            from_account_id,
            to_account_id,
            key,
        )
        assert created.status_code == 201, created.text
        transfer_id = created.json()["id"]
        outbound_id = created.json()["outbound_operation_id"]
        inbound_id = created.json()["inbound_operation_id"]
        assert outbound_id is not None and inbound_id is not None
        update_response = client.put(
            f"/operations/{outbound_id}",
            json={"amount": "-1.00"},
            headers=admin_headers,
        )
        reversal_response = client.post(
            f"/operations/{outbound_id}/reverse",
            headers=admin_headers,
        )
    assert update_response.status_code == 405, update_response.text
    assert reversal_response.status_code == 422, reversal_response.text

    with engine.connect() as conn:
        operation_rows = conn.execute(
            text(
                "SELECT id, amount, status FROM operations "
                "WHERE id IN (:outbound_id, :inbound_id) ORDER BY id"
            ),
            {"outbound_id": outbound_id, "inbound_id": inbound_id},
        ).all()
        transfer = conn.execute(
            text("SELECT amount, status FROM transfers WHERE id = :transfer_id"),
            {"transfer_id": transfer_id},
        ).one()
    assert [row.status for row in operation_rows] == ["POSTED", "POSTED"]
    assert [row.amount for row in operation_rows] == [Decimal("-77.35"), Decimal("77.35")]
    assert transfer == (Decimal("77.35"), "POSTED")

    for query, params in (
        ("UPDATE operations SET amount = -1.00 WHERE id = :id", {"id": outbound_id}),
        ("UPDATE transfers SET amount = 1.00 WHERE id = :id", {"id": transfer_id}),
    ):
        _assert_integrity_error_without_persisting(engine, query, params)
    _assert_integrity_error_without_persisting(
        engine,
        "UPDATE operations SET status = 'REVERSED' WHERE id = :id",
        {"id": outbound_id},
    )


