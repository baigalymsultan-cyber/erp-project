"""Provenance DELETE guard — TD-019 step 2, FINANCE-OPS-001 Absolute Rule 12.

operations/transfers keep ON DELETE SET NULL links to raw_kaspi_payloads
and import_batches. These provenance tables now carry the same
fn_forbid_delete() BEFORE DELETE trigger as the financial tables, so a
plain DELETE cannot silently destroy the provenance chain behind an
amount. The migrator-only maintenance hatch is unchanged.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

import conftest as _conftest  # purge order lives here


@pytest.fixture(scope="module")
def ip_id(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ip_entities (name, bin) "
                "VALUES ('PROVTEST ИП', '900000000002') ON CONFLICT (bin) DO NOTHING"
            )
        )
        return conn.execute(
            text("SELECT id FROM ip_entities WHERE bin = '900000000002'")
        ).scalar_one()


def _insert_payload(conn, ip_id) -> int:
    return conn.execute(
        text(
            "INSERT INTO raw_kaspi_payloads (ip_id, request_type, payload) "
            "VALUES (:ip, 'TEST', '{\"a\": 1}'::jsonb) RETURNING id"
        ),
        {"ip": ip_id},
    ).scalar_one()


def _insert_batch(conn) -> int:
    return conn.execute(
        text(
            "INSERT INTO import_batches (source_type, source_name) "
            "VALUES ('MANUAL', 'prov-test') RETURNING id"
        )
    ).scalar_one()


def _trigger_enabled(conn, table: str) -> bool:
    return (
        conn.execute(
            text(
                "SELECT tgenabled FROM pg_trigger "
                "WHERE tgrelid = ('public.' || :t)::regclass "
                "AND tgname = ('trg_' || :t || '_forbid_delete') "
                "AND NOT tgisinternal"
            ),
            {"t": table},
        ).scalar_one_or_none()
        in ("O", "A")
    )


@pytest.mark.parametrize("table", ["raw_kaspi_payloads", "import_batches"])
def test_provenance_delete_guard_trigger_installed(engine, table):
    """The forbid-delete trigger exists and is enabled on provenance tables."""
    with engine.connect() as conn:
        assert _trigger_enabled(conn, table), f"trigger missing on {table}"


def test_raw_payload_plain_delete_is_rejected(engine, ip_id):
    """TD-019 — erp_app cannot DELETE a raw Kaspi payload row."""
    with engine.begin() as conn:
        pid = _insert_payload(conn, ip_id)
    with pytest.raises(Exception), engine.begin() as conn:
        conn.execute(text("DELETE FROM raw_kaspi_payloads WHERE id = :i"), {"i": pid})


def test_import_batch_plain_delete_is_rejected(engine):
    """TD-019 — erp_app cannot DELETE an import batch row."""
    with engine.begin() as conn:
        bid = _insert_batch(conn)
    with pytest.raises(Exception), engine.begin() as conn:
        conn.execute(text("DELETE FROM import_batches WHERE id = :i"), {"i": bid})


def test_provenance_delete_via_migrator_hatch(engine, ip_id):
    """The maintenance hatch still works for the migrator role only."""
    with engine.begin() as conn:
        current_user = conn.execute(text("SELECT current_user")).scalar_one()
    if current_user != "erp_migrator":
        pytest.skip("hatch requires the migrator role; connected as " + current_user)
    with engine.begin() as conn:
        pid = _insert_payload(conn, ip_id)
        bid = _insert_batch(conn)
        conn.execute(text("SET LOCAL app.allow_financial_delete = 'on'"))
        conn.execute(text("DELETE FROM raw_kaspi_payloads WHERE id = :i"), {"i": pid})
        conn.execute(text("DELETE FROM import_batches WHERE id = :i"), {"i": bid})


def test_purge_order_deletes_children_before_provenance_parents():
    """TD-019 — purge must not null out provenance links before children go.

    operations/transfers/kaspi_payouts reference raw_kaspi_payloads and
    import_batches; raw_kaspi_payloads references sync_log. The purge order
    must list children first so the SET NULL FKs never fire during purge.
    """
    order = list(_conftest._BUSINESS_TABLES_PURGE_ORDER)
    for child in ("operations", "transfers", "kaspi_payouts"):
        for parent in ("raw_kaspi_payloads", "import_batches"):
            assert order.index(child) < order.index(parent), (
                f"purge order: {child} must come before {parent}"
            )
    assert order.index("raw_kaspi_payloads") < order.index("sync_log"), (
        "purge order: raw_kaspi_payloads must come before sync_log"
    )
    assert order.index("sync_log") < order.index("ip_entities")
