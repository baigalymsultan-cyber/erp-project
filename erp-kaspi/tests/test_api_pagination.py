"""§74 pagination tests — unbounded list endpoints cap their responses.

The payouts listing is the first paginated endpoint (TD-PERF-1): the raw
table can grow unboundedly with sync history, so a missing limit is an
application-level DoS, not a style issue.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.api import app

# per-run digits: each test file derives its own unique БИН suffix

#: unique run suffix — repeated runs stay inside the unique constraints
RUN = uuid.uuid4().hex[:12]


@pytest.fixture
def ip_id(engine):
    """A unique-per-test ИП: rows never leak between the three tests."""
    bin_digits = str(int(uuid.uuid4().hex[:8], 16) % 100_000_000_000).zfill(11)
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) "
                "VALUES (:n, :b) "
                "ON CONFLICT (bin) DO NOTHING "
                "RETURNING id"
            ),
            {"n": f"PAGINATION ИП {RUN}-{bin_digits[:4]}", "b": f"7{bin_digits}"},
        ).scalar_one()


def _seed_payouts(engine: Engine, ip_id: int, n: int) -> None:
    batch = uuid.uuid4().hex[:8]  # every seeding call is its own batch
    with engine.begin() as conn:
        for i in range(n):
            conn.execute(
                text(
                    "INSERT INTO kaspi_payouts (ip_id, payout_date, gross_amount, "
                    "commission_amount, net_amount, status, external_payout_id) "
                    "VALUES (:ip, CURRENT_DATE - :d, 1000.00, 100.00, 900.00, 'RECEIVED', :ext)"
                ),
                {"ip": ip_id, "d": i, "ext": f"PERF-PAY-{batch}-{i}"},
            )


def test_payouts_default_limit_100(engine, applied_schema, ip_id, admin_headers):
    """/kaspi/payouts must cap the response even with more rows present."""
    _seed_payouts(engine, ip_id, 105)
    with TestClient(app) as client:
        got = client.get("/kaspi/payouts", params={"ip_id": ip_id}, headers=admin_headers).json()
    assert got["count"] == 100, "§74: the default page size caps the response"
    assert "next_offset" in got, "§74: the response advertises the next page"


def test_payouts_explicit_limit_is_capped(engine, applied_schema, ip_id, admin_headers):
    """A client may not raise the page size above the server cap."""
    _seed_payouts(engine, ip_id, 300)
    with TestClient(app) as client:
        got = client.get(
            "/kaspi/payouts",
            params={"ip_id": ip_id, "limit": 10_000},
            headers=admin_headers,
        ).json()
    assert got["count"] == 200, "§74: limit is capped at the server maximum"


def test_payouts_offset_pages_are_stable(engine, applied_schema, ip_id, admin_headers):
    """offset/limit paging walks the full set without duplicates (§74)."""
    _seed_payouts(engine, ip_id, 45)
    seen: list[int] = []
    with TestClient(app) as client:
        offset = 0
        while True:
            got = client.get(
                "/kaspi/payouts",
                params={"ip_id": ip_id, "limit": 20, "offset": offset},
                headers=admin_headers,
            ).json()
            seen.extend(row["id"] for row in got["payouts"])
            if not got["payouts"]:
                break
            offset += 20
    assert len(seen) == 45, "§74: paging covers the whole set exactly once"
    assert len(set(seen)) == 45, "§74: no duplicate rows across pages"
