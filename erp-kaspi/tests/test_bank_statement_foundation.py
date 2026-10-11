"""Bank statement foundation tests (G-BANK-2..5) — TDD per impl spec §7.

Covers: MT940 parser (§7.2), fingerprint (§7.3), idempotency matrix I-1..I-8
(§7.1), service integration (§7.4), trigger immutability, matcher v1
(T1..T4/T10), concurrency (§7.6), and the /bank/* API.
"""

from __future__ import annotations

import random
import threading
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import app
from app.models.bank import BankStatementLine
from app.schemas import OperationCreate, OperationDirection
from app.services import add_operation
from app.services.bank_import import (
    MT940StatementAdapter,
    NormalizedBankTransaction,
    PostingPolicy,
    StatementImportService,
    commission_knp_codes,
    core_fingerprint,
    post_unmatched_as_operations,
)
from app.services.reconciliation import BankReconciliationService

pytestmark = pytest.mark.usefixtures("applied_schema")


# --------------------------------------------------------------------------- #
# Fixtures & helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def bank_world(engine):
    """Fresh ИП + bank account; returns ids dict. Commits (service commits too)."""
    suffix = uuid.uuid4().hex[:8]
    with Session(engine) as session:
        ip_id = session.execute(
            text("INSERT INTO ip_entities (name, bin) VALUES (:n, :b) RETURNING id"),
            {
                "n": f"BANK-IP-{suffix}",
                "b": f"{random.randint(10**11, 10**12 - 1):012d}",
            },
        ).scalar_one()
        account_id = session.execute(
            text(
                "INSERT INTO accounts (ip_id, type, name, bank_code, is_active) "
                "VALUES (:ip, 'BANK', :n, 'HALYK', TRUE) RETURNING id"
            ),
            {"ip": ip_id, "n": f"BANK-ACC-{suffix}"},
        ).scalar_one()
        session.commit()
    return {"ip_id": ip_id, "account_id": account_id, "suffix": suffix}


def make_mt940(
    *,
    uid: str,
    account: str = "KZ123456789012345678",
    lines: list[tuple[str, str, str, str]] | None = None,
    opening: str = "100000,00",
    closing: str | None = None,
    extra_86: dict[int, str] | None = None,
) -> bytes:
    """Build a synthetic Halyk MT940.

    lines: list of (value_date YYMMDD, dc_mark, amount, reference).
    """
    if lines is None:
        lines = [("260102", "D", "15000,00", "PAY-001"), ("260103", "C", "25000,00", "PAY-002")]
    body = [
        f":20:STMT-{uid}",
        f":25:{account}",
        ":28C:00001/001",
        f":60F:C260101KZT{opening}",
    ]
    for i, (vdate, dc, amount, ref) in enumerate(lines):
        body.append(f":61:{vdate}0102{dc}{amount}NTRF{ref}//BREF-{ref}")
        narrative = (extra_86 or {}).get(i, f"Назначение {ref}")
        body.append(f":86:{narrative}")
    if closing is None:
        # foot the statement automatically (MT940: C=credit, D=debit balance)
        total = Decimal(opening.replace(",", "."))
        for _vdate, dc, amount, _ref in lines:
            amt = Decimal(amount.replace(",", "."))
            total += -amt if dc in ("D", "EC", "ED") else amt
        sign = "C" if total >= 0 else "D"
        closing = f"{sign}260103KZT{abs(total):.2f}".replace(".", ",")
    else:
        # explicit override is the amount part only (test-injected mismatch)
        closing = f"C260103KZT{closing}"
    body.append(f":62F:{closing}")
    return "\r\n".join(body).encode("utf-8")


def _import(engine, *, ip_id, account_id=None, content, filename="stmt.mt940", **kwargs) -> object:
    from app.services.bank_import import ImportResult

    with Session(engine) as session:
        service = StatementImportService()
        result: ImportResult = service.import_statement(
            session,
            ip_id=ip_id,
            account_id=account_id,
            content=content,
            filename=filename,
            **kwargs,
        )
    return result


# --------------------------------------------------------------------------- #
# §7.2 — MT940 parser
# --------------------------------------------------------------------------- #


class TestMT940Parser:
    def test_debit_credit_markers(self):
        adapter = MT940StatementAdapter()
        content = make_mt940(
            uid="m1",
            lines=[
                ("260102", "D", "100,00", "R1"),
                ("260102", "C", "200,00", "R2"),
                ("260102", "EC", "300,00", "R3"),
                ("260102", "ED", "400,00", "R4"),
                ("260102", "RC", "500,00", "R5"),
            ],
            opening="0,00",
        )
        parsed = adapter.parse(content, "s.mt940")
        directions = [tx.direction for tx in parsed.transactions]
        assert directions == ["OUT", "IN", "OUT", "OUT", "IN"]
        assert parsed.provider_code == "HALYK"
        assert parsed.provider_account == "KZ123456789012345678"
        assert parsed.statement_no == "00001/001"

    def test_comma_decimals_and_balances(self):
        adapter = MT940StatementAdapter()
        parsed = adapter.parse(make_mt940(uid="m2"), "s.mt940")
        assert parsed.opening_balance == Decimal("100000.00")
        assert parsed.closing_balance == Decimal("110000.00")
        amounts = [tx.amount for tx in parsed.transactions]
        assert amounts == [Decimal("15000.00"), Decimal("25000.00")]

    def test_knp_and_iin_from_86(self):
        adapter = MT940StatementAdapter()
        content = make_mt940(
            uid="m3",
            extra_86={0: "Оплата услуг КНП 911 БИН 123456789012 доп текст"},
        )
        parsed = adapter.parse(content, "s.mt940")
        tx = parsed.transactions[0]
        assert tx.knp_code == "911"
        assert tx.counterparty_iin == "123456789012"
        assert "Оплата услуг" in (tx.payment_purpose or "")
        assert tx.provider_extra["mt940_86_raw"].startswith("Оплата услуг")

    def test_unknown_dc_mark_skipped_with_warning(self):
        adapter = MT940StatementAdapter()
        content = make_mt940(
            uid="m4", lines=[("260102", "X", "100,00", "R1"), ("260103", "C", "50,00", "R2")]
        )
        parsed = adapter.parse(content, "s.mt940")
        assert len(parsed.transactions) == 1
        assert parsed.transactions[0].document_number == "R2"
        assert any("X" in w for w in parsed.warnings)

    def test_can_parse_rejects_non_mt940(self):
        adapter = MT940StatementAdapter()
        assert not adapter.can_parse(b"date,amount\n2026-01-01,100", "s.csv")
        assert not adapter.can_parse(b"\x00\x01\x02binary", "s.bin")
        assert adapter.can_parse(make_mt940(uid="m5"), "s.mt940")

    def test_document_and_bank_reference(self):
        adapter = MT940StatementAdapter()
        parsed = adapter.parse(make_mt940(uid="m6"), "s.mt940")
        tx = parsed.transactions[0]
        assert tx.document_number == "PAY-001"
        assert tx.bank_reference == "BREF-PAY-001"


# --------------------------------------------------------------------------- #
# §7.3 — fingerprint
# --------------------------------------------------------------------------- #


def _tx(**overrides) -> NormalizedBankTransaction:
    base = dict(
        provider_code="HALYK",
        provider_account="KZ1",
        value_date=date(2026, 1, 15),
        entry_date=date(2026, 1, 16),
        amount=Decimal("12500.00"),
        currency="KZT",
        direction="OUT",
        document_number="DOC-1",
        bank_reference="BR-1",
        raw_line=":61:...",
        line_no=1,
    )
    base.update(overrides)
    return NormalizedBankTransaction(**base)


class TestFingerprint:
    def test_amount_normalisation(self):
        assert core_fingerprint(_tx(amount=Decimal("12500"))) == core_fingerprint(
            _tx(amount=Decimal("12500.00"))
        )

    def test_provider_scopes_fingerprint(self):
        assert core_fingerprint(_tx(provider_code="HALYK")) != core_fingerprint(
            _tx(provider_code="EPAY")
        )

    def test_entry_date_ignored(self):
        assert core_fingerprint(_tx(entry_date=date(2026, 1, 16))) == core_fingerprint(
            _tx(entry_date=date(2026, 1, 20))
        )

    def test_value_date_matters(self):
        assert core_fingerprint(_tx(value_date=date(2026, 1, 15))) != core_fingerprint(
            _tx(value_date=date(2026, 1, 16))
        )

    def test_is_sha256_hex(self):
        fp = core_fingerprint(_tx())
        assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)


# --------------------------------------------------------------------------- #
# §7.1 — idempotency matrix
# --------------------------------------------------------------------------- #


class TestIdempotency:
    def test_i1_same_file_twice(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        content = make_mt940(uid=f"i1-{bank_world['suffix']}")
        r1 = _import(engine, ip_id=ip_id, content=content)
        assert r1.status == "SUCCESS", r1.errors
        assert r1.rows_inserted == 2 and r1.rows_duplicates == 0
        r2 = _import(engine, ip_id=ip_id, content=content)
        assert r2.status == "SUCCESS"
        assert r2.rows_inserted == 0
        assert r2.rows_duplicates == r2.rows_total == 2
        assert r2.statement_id == r1.statement_id
        with Session(engine) as s:
            n = s.execute(
                text("SELECT count(*) FROM raw_bank_statements WHERE ip_id = :ip"),
                {"ip": ip_id},
            ).scalar_one()
            assert n == 1

    def test_i2_overlapping_files_share_lines(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        uid = f"i2-{bank_world['suffix']}"
        shared = [("260102", "D", "15000,00", "SHARED-1")]
        c1 = make_mt940(uid=uid + "-a", lines=[*shared, ("260103", "C", "100,00", "A-ONLY")])
        c2 = make_mt940(uid=uid + "-b", lines=[*shared, ("260104", "C", "200,00", "B-ONLY")])
        r1 = _import(engine, ip_id=ip_id, content=c1, filename="a.mt940")
        assert r1.rows_inserted == 2, r1.errors
        r2 = _import(engine, ip_id=ip_id, content=c2, filename="b.mt940")
        assert r2.rows_inserted == 1, r2.errors  # SHARED-1 is a duplicate
        assert r2.rows_duplicates == 1

    def test_i3_concurrent_imports(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        content = make_mt940(uid=f"i3-{bank_world['suffix']}")
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}
        errors: list[str] = []

        def worker(name: str):
            try:
                barrier.wait(timeout=30)
                with Session(engine) as session:
                    service = StatementImportService()
                    results[name] = service.import_statement(
                        session,
                        ip_id=ip_id,
                        account_id=None,
                        content=content,
                        filename="race.mt940",
                    )
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not errors, errors
        assert len(results) == 2
        for result in results.values():
            assert result.status == "SUCCESS", result.errors
        with Session(engine) as s:
            n_lines = s.execute(
                text("SELECT count(*) FROM bank_statement_lines WHERE ip_id = :ip"),
                {"ip": ip_id},
            ).scalar_one()
            n_raw = s.execute(
                text("SELECT count(*) FROM raw_bank_statements WHERE ip_id = :ip"),
                {"ip": ip_id},
            ).scalar_one()
        assert n_lines == 2, "no doubled lines"
        assert n_raw == 1, "one raw statement"

    def test_i4_crash_retry_resumes(self, engine, bank_world):
        import hashlib

        ip_id = bank_world["ip_id"]
        uid = f"i4-{bank_world['suffix']}"
        content = make_mt940(
            uid=uid,
            lines=[
                ("260102", "D", "1000,00", "L1"),
                ("260103", "D", "2000,00", "L2"),
                ("260104", "D", "3000,00", "L3"),
            ],
        )
        # Real fingerprint of L1 so the retry dedups it.
        adapter = MT940StatementAdapter()
        parsed = adapter.parse(content, "crash.mt940")
        fp_l1 = adapter.fingerprint(parsed.transactions[0])
        # Simulate a crash: raw row committed, only 1 of 3 lines, processed_at NULL.
        file_hash = hashlib.sha256(content).hexdigest()
        with Session(engine) as s:
            raw_id = s.execute(
                text(
                    "INSERT INTO raw_bank_statements (ip_id, provider_code, file_name, "
                    "file_hash, content, import_batch_id) "
                    "VALUES (:ip, 'HALYK', 'crash.mt940', :fh, :c, NULL) RETURNING id"
                ),
                {"ip": ip_id, "fh": file_hash, "c": content},
            ).scalar_one()
            s.execute(
                text(
                    "INSERT INTO bank_statement_lines (ip_id, statement_id, provider_code, "
                    "provider_account, value_date, amount, currency, direction, "
                    "document_number, raw_line, line_no, line_fingerprint) "
                    "VALUES (:ip, :raw, 'HALYK', 'KZ1', '2026-01-02', 1000, 'KZT', "
                    "'OUT', 'L1', 'x', 1, :fp)"
                ),
                {"ip": ip_id, "raw": raw_id, "fp": fp_l1},
            )
            s.commit()
        result = _import(engine, ip_id=ip_id, content=content, filename="crash.mt940")
        assert result.status == "SUCCESS", result.errors
        assert result.statement_id == raw_id, "resumed the crashed statement"
        assert result.rows_inserted == 2, result.errors  # L2, L3
        assert result.rows_duplicates == 1  # L1

    def test_i5_partial_batch_bad_line(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        content = make_mt940(
            uid=f"i5-{bank_world['suffix']}",
            lines=[("260102", "D", "1000,00", "OK-1"), ("260103", "C", "500,00", "OK-2")],
        )
        # Append a broken :61: (zero amount is skipped by the parser with a warning).
        content = content.replace(b":62F:", b":61:2601040102D0,00NTRFZERO-1\r\n:62F:")
        result = _import(engine, ip_id=ip_id, content=content)
        assert result.status == "PARTIAL", result.errors
        assert result.rows_inserted == 2
        assert result.rows_skipped >= 1
        assert any("ZERO-1" in str(e) or "amount" in str(e).lower() for e in result.errors)

    def test_i6_balance_control_mismatch(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        content = make_mt940(uid=f"i6-{bank_world['suffix']}", closing="999999,00")
        result = _import(engine, ip_id=ip_id, content=content)
        assert result.status == "FAILED"
        assert result.balance_control is not None and not result.balance_control["ok"]
        with Session(engine) as s:
            batch_status = s.execute(
                text("SELECT status FROM import_batches WHERE id = :b"),
                {"b": result.batch_id},
            ).scalar_one()
            assert batch_status == "FAILED"
            # Lines are NOT rolled back (external fact is kept)...
            n = s.execute(
                text("SELECT count(*) FROM bank_statement_lines WHERE statement_id = :s"),
                {"s": result.statement_id},
            ).scalar_one()
            assert n == 2
        # ...but posting is blocked.
        with Session(engine) as s:
            line_ids = (
                s.execute(
                    text("SELECT id FROM bank_statement_lines WHERE statement_id = :s"),
                    {"s": result.statement_id},
                )
                .scalars()
                .all()
            )
        created = post_unmatched_as_operations(
            Session(engine), ip_id=ip_id, line_ids=line_ids, policy=PostingPolicy()
        )
        assert created == []

    def test_i7_repost_is_idempotent(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        uid = f"i7-{bank_world['suffix']}"
        content = make_mt940(
            uid=uid,
            lines=[("260102", "D", "1500,00", "FEE-1")],
            extra_86={0: "Комиссия банка КНП 999"},
        )
        result = _import(engine, ip_id=ip_id, content=content)
        assert result.status == "SUCCESS", result.errors
        with Session(engine) as s:
            line_id = s.execute(
                text("SELECT id FROM bank_statement_lines WHERE statement_id = :s"),
                {"s": result.statement_id},
            ).scalar_one()
        policy = PostingPolicy(commission_knp_codes=frozenset({"999"}))
        with Session(engine) as s:
            created1 = post_unmatched_as_operations(
                s, ip_id=ip_id, line_ids=[line_id], policy=policy
            )
            assert len(created1) == 1
            n_ops = s.execute(text("SELECT count(*) FROM operations")).scalar_one()
            # Second post: the line is CONFIRMED already -> not selected.
            created2 = post_unmatched_as_operations(
                s, ip_id=ip_id, line_ids=[line_id], policy=policy
            )
            assert created2 == []
            assert s.execute(text("SELECT count(*) FROM operations")).scalar_one() == n_ops

    def test_i8_two_providers_two_facts(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        uid = f"i8-{bank_world['suffix']}"
        content = make_mt940(uid=uid, lines=[("260102", "D", "5000,00", "SAME")])
        r1 = _import(engine, ip_id=ip_id, content=content, filename="halyk.mt940")
        assert r1.rows_inserted == 1

        # Same economic fact via a second adapter namespace -> separate line.
        class EpayAdapter(MT940StatementAdapter):
            provider_code = "EPAY"

        with Session(engine) as s:
            service = StatementImportService(adapters=[EpayAdapter()])
            r2 = service.import_statement(
                s, ip_id=ip_id, account_id=None, content=content, filename="epay.xlsx"
            )
            assert r2.rows_inserted == 1, r2.errors
            n = s.execute(
                text("SELECT count(*) FROM bank_statement_lines WHERE ip_id = :ip"),
                {"ip": ip_id},
            ).scalar_one()
            assert n == 2


# --------------------------------------------------------------------------- #
# §7.4 — service integration
# --------------------------------------------------------------------------- #


class TestService:
    def test_dry_run_rolls_back(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        content = make_mt940(uid=f"dry-{bank_world['suffix']}")
        result = _import(engine, ip_id=ip_id, content=content, dry_run=True)
        assert result.status == "DRY_RUN"
        assert result.statement_id is None
        assert result.rows_inserted == 2  # would-be inserts
        with Session(engine) as s:
            n = s.execute(
                text("SELECT count(*) FROM raw_bank_statements WHERE ip_id = :ip"),
                {"ip": ip_id},
            ).scalar_one()
            assert n == 0

    def test_resolve_account(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        account_id = bank_world["account_id"]
        service = StatementImportService()
        with Session(engine) as s:
            # Exactly one active HALYK account -> resolved.
            assert (
                service._resolve_account(
                    s,
                    ip_id=ip_id,
                    account_id=None,
                    provider_code="HALYK",
                    provider_account="KZ1",
                )
                == account_id
            )
            # Explicit foreign account -> ValueError.
            with pytest.raises(ValueError):
                service._resolve_account(
                    s,
                    ip_id=ip_id,
                    account_id=999999999,
                    provider_code="HALYK",
                    provider_account="KZ1",
                )
            # Second HALYK account -> ambiguous -> None.
            s.execute(
                text(
                    "INSERT INTO accounts (ip_id, type, name, bank_code, is_active) "
                    "VALUES (:ip, 'BANK', 'second', 'HALYK', TRUE)"
                ),
                {"ip": ip_id},
            )
            s.commit()
            assert (
                service._resolve_account(
                    s,
                    ip_id=ip_id,
                    account_id=None,
                    provider_code="HALYK",
                    provider_account="KZ1",
                )
                is None
            )

    def test_non_kzt_line_skipped(self, engine, bank_world):
        # MT940 :61: carries no currency; the adapter stamps KZT. A tx with a
        # foreign currency must be skipped with a reason (STOP condition).
        service = StatementImportService()
        tx = _tx(currency="USD")
        assert service._validate_tx(tx) is not None
        assert "OQ-014" in (service._validate_tx(tx) or "")

    def test_commission_knp_codes_union(self, engine, bank_world):
        with Session(engine) as s:
            s.execute(
                text(
                    "INSERT INTO bank_fee_knp_codes (knp_code, description) "
                    "VALUES ('911', 'test fee') ON CONFLICT DO NOTHING"
                )
            )
            s.commit()
            codes = commission_knp_codes(s, PostingPolicy(commission_knp_codes=frozenset({"999"})))
            assert {"911", "999"} <= codes


class TestImmutabilityTrigger:
    def _line_id(self, engine, bank_world) -> int:
        ip_id = bank_world["ip_id"]
        result = _import(engine, ip_id=ip_id, content=make_mt940(uid=f"trg-{bank_world['suffix']}"))
        assert result.status == "SUCCESS", result.errors
        with Session(engine) as s:
            return s.execute(
                text("SELECT id FROM bank_statement_lines WHERE statement_id = :s LIMIT 1"),
                {"s": result.statement_id},
            ).scalar_one()

    def test_update_fact_blocked(self, engine, bank_world):
        line_id = self._line_id(engine, bank_world)
        with Session(engine) as s, pytest.raises(IntegrityError):
            s.execute(
                text("UPDATE bank_statement_lines SET amount = 1 WHERE id = :i"),
                {"i": line_id},
            )
            s.flush()

    def test_update_workflow_columns_allowed(self, engine, bank_world):
        line_id = self._line_id(engine, bank_world)
        with Session(engine) as s:
            s.execute(
                text(
                    "UPDATE bank_statement_lines SET match_status = 'IGNORED', "
                    'provider_extra = provider_extra || \'{"audit": {"reason": "x"}}\' '
                    "WHERE id = :i"
                ),
                {"i": line_id},
            )
            s.commit()
            status = s.execute(
                text("SELECT match_status FROM bank_statement_lines WHERE id = :i"),
                {"i": line_id},
            ).scalar_one()
            assert status == "IGNORED"

    def test_update_fact_key_in_provider_extra_blocked(self, engine, bank_world):
        line_id = self._line_id(engine, bank_world)
        with Session(engine) as s, pytest.raises(IntegrityError):
            s.execute(
                text(
                    "UPDATE bank_statement_lines SET provider_extra = "
                    'provider_extra || \'{"mt940_txn_type": "HACK"}\' WHERE id = :i'
                ),
                {"i": line_id},
            )
            s.flush()

    def test_delete_blocked_without_escape(self, engine, bank_world):
        line_id = self._line_id(engine, bank_world)
        with Session(engine) as s, pytest.raises(IntegrityError):
            s.execute(text("DELETE FROM bank_statement_lines WHERE id = :i"), {"i": line_id})
            s.flush()

    def test_delete_allowed_with_guc_and_migrator(self, engine, bank_world):
        # The §94 purge path: GUC + erp_migrator membership (TD-003 pattern).
        line_id = self._line_id(engine, bank_world)
        import os

        from app.core.config import DatabaseSettings
        from app.core.database import create_database_engine

        env = dict(os.environ)
        password = env.get("TEST_DATABASE_MIGRATOR_PASSWORD") or env.get("DB_MIGRATOR_PASSWORD")
        assert password, "migrator password required"
        env["DATABASE_PASSWORD"] = password
        env["DATABASE_USER"] = "erp_migrator"
        env.setdefault("DATABASE_NAME", "erp_test")
        settings = DatabaseSettings.from_env(env)
        eng = create_database_engine(settings)
        try:
            with Session(eng) as s:
                s.execute(text("SET LOCAL app.allow_financial_delete = 'on'"))
                s.execute(text("DELETE FROM bank_statement_lines WHERE id = :i"), {"i": line_id})
                s.commit()
                n = s.execute(
                    text("SELECT count(*) FROM bank_statement_lines WHERE id = :i"),
                    {"i": line_id},
                ).scalar_one()
                assert n == 0
        finally:
            eng.dispose()


# --------------------------------------------------------------------------- #
# Matcher v1 (framework T1..T4, T10)
# --------------------------------------------------------------------------- #


def _make_posted_op(engine, *, ip_id, account_id, amount, op_date, external_id=None) -> int:
    type_code = "SALE_KASPI_PAY" if amount > 0 else "EXPENSE_MATERIAL"
    with Session(engine) as s:
        type_id = s.execute(
            text("SELECT id FROM operation_types WHERE code = :c"), {"c": type_code}
        ).scalar_one()
        op_id = add_operation(
            s,
            OperationCreate(
                ip_id=ip_id,
                type_id=type_id,
                direction=OperationDirection.IN if amount > 0 else OperationDirection.OUT,
                amount=Decimal(str(amount)),
                operation_date=op_date,
                accrual_date=None,
                external_order_id=external_id,
            ),
            account_id=account_id,
        )
        s.commit()
        return op_id


class TestMatcher:
    def test_t1_exact_reference(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        account_id = bank_world["account_id"]
        op_id = _make_posted_op(
            engine,
            ip_id=ip_id,
            account_id=account_id,
            amount=-500000,
            op_date=date(2026, 3, 11),
            external_id="PP-2026-042",
        )
        content = make_mt940(
            uid=f"t1-{bank_world['suffix']}",
            lines=[("260310", "D", "500000,00", "PP-2026-042")],
        )
        result = _import(engine, ip_id=ip_id, content=content)
        assert result.status == "SUCCESS", result.errors
        with Session(engine) as s:
            line_id = s.execute(
                text("SELECT id FROM bank_statement_lines WHERE statement_id = :s"),
                {"s": result.statement_id},
            ).scalar_one()
        service = BankReconciliationService()
        with Session(engine) as s:
            counts = service.suggest_matches(s, ip_id=ip_id)
            assert counts["suggested"] == 1, counts
            line = s.get(BankStatementLine, line_id)
            assert line.match_status == "SUGGESTED"
            assert line.matched_operation_id == op_id
            assert line.provider_extra["audit"]["verdict"] == "EXACT_REFERENCE_MATCH"
            confirmed = service.confirm_line(
                s, ip_id=ip_id, line_id=line_id, operation_id=op_id, user_id=None
            )
            assert confirmed.match_status == "CONFIRMED"
            s.commit()

    def test_t2_transfer_pair(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        uid = f"t2-{bank_world['suffix']}"
        c1 = make_mt940(
            uid=uid + "-a", account="KZ-ACC-1", lines=[("260310", "D", "200000,00", "T-OUT")]
        )
        c2 = make_mt940(
            uid=uid + "-b", account="KZ-ACC-2", lines=[("260310", "C", "200000,00", "T-IN")]
        )
        r1 = _import(engine, ip_id=ip_id, content=c1, filename="a.mt940")
        r2 = _import(engine, ip_id=ip_id, content=c2, filename="b.mt940")
        assert r1.status == r2.status == "SUCCESS"
        service = BankReconciliationService()
        with Session(engine) as s:
            before = s.execute(
                text("SELECT count(*) FROM transfers WHERE from_ip_id = :ip OR to_ip_id = :ip"),
                {"ip": ip_id},
            ).scalar_one()
            counts = service.suggest_matches(s, ip_id=ip_id)
            assert counts["transfer_pair"] == 2, counts
            # No operations created, no transfer auto-created: both UNMATCHED.
            n = s.execute(
                text(
                    "SELECT count(*) FROM bank_statement_lines WHERE ip_id = :ip "
                    "AND match_status = 'UNMATCHED'"
                ),
                {"ip": ip_id},
            ).scalar_one()
            assert n == 2
            after = s.execute(
                text("SELECT count(*) FROM transfers WHERE from_ip_id = :ip OR to_ip_id = :ip"),
                {"ip": ip_id},
            ).scalar_one()
            assert after == before, "matcher must never auto-create transfers"

    def test_t3_commission_post(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        content = make_mt940(
            uid=f"t3-{bank_world['suffix']}",
            lines=[("260310", "D", "1500,00", "FEE-1")],
            extra_86={0: "Комиссия за обслуживание КНП 123"},
        )
        result = _import(engine, ip_id=ip_id, content=content)
        assert result.status == "SUCCESS", result.errors
        service = BankReconciliationService()
        with Session(engine) as s:
            counts = service.suggest_matches(
                s,
                ip_id=ip_id,
            )
            # No KNP registry entry -> stays UNMATCHED/NEEDS_REVIEW.
            assert counts["needs_review"] == 1, counts
        policy = PostingPolicy(commission_knp_codes=frozenset({"123"}))
        with Session(engine) as s:
            counts = service.suggest_matches(s, ip_id=ip_id, policy=policy)
            assert counts["commission"] == 1, counts
            line_id = s.execute(
                text("SELECT id FROM bank_statement_lines WHERE statement_id = :s"),
                {"s": result.statement_id},
            ).scalar_one()
            created = post_unmatched_as_operations(
                s, ip_id=ip_id, line_ids=[line_id], policy=policy
            )
            assert len(created) == 1
            op = s.execute(
                text(
                    "SELECT amount, status, import_fingerprint, account_id "
                    "FROM operations WHERE id = :i"
                ),
                {"i": created[0]},
            ).one()
            assert op.amount == Decimal("-1500.00")
            assert op.status == "DRAFT"  # policy default
            assert op.account_id == bank_world["account_id"]
            line = s.get(BankStatementLine, line_id)
            assert line.match_status == "CONFIRMED"
            assert line.matched_operation_id == created[0]

    def test_t4_amount_alone_never_matches(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        account_id = bank_world["account_id"]
        for _ in range(3):
            _make_posted_op(
                engine,
                ip_id=ip_id,
                account_id=account_id,
                amount=1000000,
                op_date=date(2026, 3, 10),
                external_id=None,
            )
        content = make_mt940(
            uid=f"t4-{bank_world['suffix']}",
            lines=[("260310", "C", "1000000,00", "NOREF")],
        )
        result = _import(engine, ip_id=ip_id, content=content)
        assert result.status == "SUCCESS", result.errors
        service = BankReconciliationService()
        with Session(engine) as s:
            counts = service.suggest_matches(s, ip_id=ip_id)
            assert counts["suggested"] == 0, counts
            assert counts["needs_review"] == 1, counts

    def test_t10_cross_ip_isolation(self, engine, bank_world):
        ip_id = bank_world["ip_id"]
        # Operation exists only under a DIFFERENT ip.
        with Session(engine) as s:
            ip2 = s.execute(
                text(
                    "INSERT INTO ip_entities (name, bin) "
                    "VALUES ('BANK-IP2', '999999999999') RETURNING id"
                )
            ).scalar_one()
            s.commit()
        op_id = _make_posted_op(
            engine,
            ip_id=ip2,
            account_id=None,
            amount=-500000,
            op_date=date(2026, 3, 10),
            external_id="X-IP-1",
        )
        content = make_mt940(
            uid=f"t10-{bank_world['suffix']}",
            lines=[("260310", "D", "500000,00", "X-IP-1")],
        )
        result = _import(engine, ip_id=ip_id, content=content)
        assert result.status == "SUCCESS", result.errors
        service = BankReconciliationService()
        with Session(engine) as s:
            counts = service.suggest_matches(s, ip_id=ip_id)
            assert counts["suggested"] == 0, counts
            # And confirm across IPs is refused.
            line_id = s.execute(
                text("SELECT id FROM bank_statement_lines WHERE statement_id = :s"),
                {"s": result.statement_id},
            ).scalar_one()
            with pytest.raises(ValueError):
                service.confirm_line(
                    s, ip_id=ip_id, line_id=line_id, operation_id=op_id, user_id=None
                )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def _api_world(client, admin_headers, engine, bank_world):
    ip_id = bank_world["ip_id"]
    content = make_mt940(uid=f"api-{bank_world['suffix']}")
    r = client.post(
        "/bank/statements/import",
        files={"file": ("stmt.mt940", content, "text/plain")},
        data={"ip_id": str(ip_id)},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestBankApi:
    def test_import_and_list(self, engine, bank_world, admin_headers):
        with TestClient(app) as client:
            body = _api_world(client, admin_headers, engine, bank_world)
            assert body["status"] == "SUCCESS"
            assert body["rows_inserted"] == 2
            r = client.get("/bank/statements", headers=admin_headers)
            assert r.status_code == 200, r.text
            assert r.json()["total"] >= 1
            sid = body["statement_id"]
            r = client.get(f"/bank/statements/{sid}/lines", headers=admin_headers)
            assert r.status_code == 200, r.text
            items = r.json()["items"]
            assert len(items) == 2
            assert items[0]["match_status"] == "UNMATCHED"

    def test_suggest_confirm_ignore_flow(self, engine, bank_world, admin_headers):
        ip_id = bank_world["ip_id"]
        account_id = bank_world["account_id"]
        op_id = _make_posted_op(
            engine,
            ip_id=ip_id,
            account_id=account_id,
            amount=-15000,
            op_date=date(2026, 1, 3),
            external_id="PAY-001",
        )
        with TestClient(app) as client:
            body = _api_world(client, admin_headers, engine, bank_world)
            sid = body["statement_id"]
            r = client.post(
                "/bank/lines/suggest-matches",
                params={"ip_id": ip_id},
                json={"statement_id": sid},
                headers=admin_headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["suggested"] >= 1
            r = client.get(
                f"/bank/statements/{sid}/lines",
                params={"match_status": "SUGGESTED"},
                headers=admin_headers,
            )
            line_id = r.json()["items"][0]["id"]
            r = client.post(
                f"/bank/lines/{line_id}/confirm",
                json={"operation_id": op_id},
                headers=admin_headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["match_status"] == "CONFIRMED"
            # Second line: ignore requires a reason.
            r = client.get(f"/bank/statements/{sid}/lines", headers=admin_headers)
            other = next(i for i in r.json()["items"] if i["match_status"] == "UNMATCHED")
            r = client.post(
                f"/bank/lines/{other['id']}/ignore",
                json={"reason": "short"},
                headers=admin_headers,
            )
            assert r.status_code == 422, r.text
            r = client.post(
                f"/bank/lines/{other['id']}/ignore",
                json={"reason": "owner personal expense, out of scope"},
                headers=admin_headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["match_status"] == "IGNORED"

    def test_cross_ip_forbidden(self, engine, bank_world, admin_headers):
        # A restricted user of another ИП cannot import into this ИП.
        with Session(engine) as s:
            ip2 = s.execute(
                text(
                    "INSERT INTO ip_entities (name, bin) "
                    "VALUES ('BANK-IPX', '888888888888') RETURNING id"
                )
            ).scalar_one()
            emp = s.execute(
                text(
                    "INSERT INTO employees (full_name, assigned_ip_id, payment_type, base_salary) "
                    "VALUES ('ApiAcct', :ip, 'SALARY', 0) RETURNING id"
                ),
                {"ip": ip2},
            ).scalar_one()
            from app.core.security import hash_password

            s.execute(
                text(
                    "INSERT INTO users (username, password_hash, employee_id) "
                    "VALUES ('bank-acct-x', :h, :e)"
                ),
                {"h": hash_password("testpw123"), "e": emp},
            )
            role_id = s.execute(text("SELECT id FROM roles WHERE code = 'ACCOUNTANT'")).scalar_one()
            s.execute(
                text(
                    "INSERT INTO user_roles (user_id, role_id) "
                    "SELECT id, :r FROM users WHERE username = 'bank-acct-x'"
                ),
                {"r": role_id},
            )
            s.commit()
        with TestClient(app) as client:
            r = client.post(
                "/auth/login", json={"username": "bank-acct-x", "password": "testpw123"}
            )
            assert r.status_code == 200, r.text
            headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
            content = make_mt940(uid=f"xip-{bank_world['suffix']}")
            r = client.post(
                "/bank/statements/import",
                files={"file": ("s.mt940", content, "text/plain")},
                data={"ip_id": str(bank_world["ip_id"])},
                headers=headers,
            )
            assert r.status_code == 403, r.text

    def test_post_commission_endpoint(self, engine, bank_world, admin_headers):
        ip_id = bank_world["ip_id"]
        content = make_mt940(
            uid=f"api-post-{bank_world['suffix']}",
            lines=[("260102", "D", "777,00", "FEE-9")],
            extra_86={0: "Комиссия КНП 555"},
        )
        with TestClient(app) as client:
            r = client.post(
                "/bank/statements/import",
                files={"file": ("s.mt940", content, "text/plain")},
                data={"ip_id": str(ip_id)},
                headers=admin_headers,
            )
            assert r.status_code == 200, r.text
            sid = r.json()["statement_id"]
            r = client.get(f"/bank/statements/{sid}/lines", headers=admin_headers)
            line_id = r.json()["items"][0]["id"]
            r = client.post(
                "/bank/lines/post",
                params={"ip_id": ip_id},
                json={"line_ids": [line_id], "policy": {"commission_knp_codes": ["555"]}},
                headers=admin_headers,
            )
            assert r.status_code == 200, r.text
            assert len(r.json()["created_operation_ids"]) == 1
