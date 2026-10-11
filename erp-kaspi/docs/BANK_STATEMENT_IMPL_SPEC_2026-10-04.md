# Bank Statement Pipeline — Implementation Spec (G-BANK-2..5)

**Дата:** 2026-10-04
**Статус:** implementation-ready spec. Код не изменён.
**База:** `docs/BANK_ADAPTER_DESIGN_2026-10-04.md` (design) — этот документ доводит его до уровня "можно писать код", все факты сверены с `database/schema.sql`, `etl/import_excel.py`, `alembic/versions/`, `app/models/`, `app/schemas/`, `app/services/`.
**Ограничения:** никаких выдуманных банковских API. Только файловый импорт (MT940/Excel/CSV). KASPI_OQ_003 действует.

---

## 0. Проверенные предусловия (evidence, не assumptions)

| # | Факт | Источник |
|---|---|---|
| 1 | `import_batches.source_type` CHECK включает `'BANK'` | `database/schema.sql:1152` |
| 2 | `import_batches` имеет `source_hash CHAR(64)`, счётчики `rows_total/inserted/skipped/duplicates/errors`, статусы `RUNNING/SUCCESS/PARTIAL/FAILED/DRY_RUN` | `database/schema.sql:1152-1185` |
| 3 | `accounts.bank_code VARCHAR(20) NULL` существует в DDL | `database/schema.sql:1100` (миграция `20261004_0017`) |
| 4 | **`Account` SQLAlchemy-модель НЕ имеет `bank_code`** — model/schema drift из G-BANK-1, чинится в этой волне (§6.1) | `app/models/reference.py:200` — поле отсутствует |
| 5 | `operations.account_id BIGINT NULL` + композитный FK `fk_operations_account_ip (account_id, ip_id) → accounts(id, ip_id)` | `database/schema.sql` (operations) |
| 6 | `operations.raw_payload_id → raw_kaspi_payloads(id)` — Kaspi-специфично, для банковских строк не переиспользуется | `database/schema.sql` (operations) |
| 7 | `operations.import_batch_id → import_batches(id)`, `import_fingerprint CHAR(64) NULL` | `database/schema.sql` (operations) |
| 8 | `operations.accrual_date DATE NULL` — pure settlement возможен | `database/schema.sql` (operations) |
| 9 | `EXPENSE_BANK_FEE` отсутствует везде (seed, код) — нужен seed миграцией | grep по `database/schema.sql`, `app/` — 0 совпадений |
| 10 | F-ETL-008: per-row `session.begin_nested()` + `IntegrityError`-перехват на `uq_*_fingerprint` | `etl/import_excel.py:334-345` |
| 11 | `row_fingerprint`: SHA-256 от canonical JSON, сумма нормализована к 2 знакам | `etl/import_excel.py:111-126` |
| 12 | P0-2: batch-проверка fingerprint по 1000 + chunked commit | `etl/import_excel.py:255-270, 360-380` |
| 13 | DI-005: `fn_settlement_links_immutable()` + GUC `app.allow_financial_delete` escape | `database/schema.sql:1651` |
| 14 | **DI-005 escape слабее `fn_forbid_delete()`**: проверяет только GUC, без `pg_has_role(...,'erp_migrator')`. Новый триггер — по сильному паттерну (§2.3) | `database/schema.sql` (обе функции) |
| 15 | Baseline `_FUNCTIONS` в `alembic/versions/20260828_0000_0001_baseline_schema.py:143` — новая функция триггера добавляется туда (прецедент `fbbb160`) | файл |
| 16 | `transfers` имеет `from_account_id`/`to_account_id` — детект переводов между своими счетами | `database/schema.sql` (transfers) |
| 17 | `OperationCreate` НЕ имеет `account_id` — постинг на счёт требует решения (§4.3) | `app/schemas/__init__.py` |
| 18 | Следующий revision: `20261004_0018_*`, `down_revision = "0017_accounts_bank_code"` | `alembic/versions/` |

---

## 1. DDL — `raw_bank_statements` + `bank_statement_lines`

Стиль — как остальной `schema.sql`: `IF NOT EXISTS`, именованные CONSTRAINT, `COMMENT ON`.

### 1.1 `raw_bank_statements`

```sql
CREATE TABLE IF NOT EXISTS raw_bank_statements (
    id                  BIGSERIAL       PRIMARY KEY,
    ip_id               BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    provider_code       VARCHAR(20)     NOT NULL,
    account_id          BIGINT          NULL REFERENCES accounts (id) ON DELETE RESTRICT,
    file_name           TEXT            NOT NULL,
    file_hash           CHAR(64)        NOT NULL,
    content             BYTEA           NOT NULL,
    content_text        TEXT            NULL,
    statement_no        VARCHAR(50)     NULL,
    period_from         DATE            NULL,
    period_to           DATE            NULL,
    opening_balance     NUMERIC(15,2)   NULL,
    closing_balance     NUMERIC(15,2)   NULL,
    currency            VARCHAR(3)      NULL,
    import_batch_id     BIGINT          NULL REFERENCES import_batches (id) ON DELETE RESTRICT,
    received_at         TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at        TIMESTAMPTZ     NULL,
    created_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT chk_raw_bank_statements_provider
        CHECK (provider_code IN ('HALYK', 'FREEDOM', 'EPAY', 'KASPI_PAY', 'MANUAL')),
    CONSTRAINT chk_raw_bank_statements_file_hash
        CHECK (file_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_raw_bank_statements_currency
        CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_raw_bank_statements_period
        CHECK (period_to IS NULL OR period_from IS NULL OR period_to >= period_from),
    CONSTRAINT uq_raw_bank_statements_file
        UNIQUE (ip_id, provider_code, file_hash)
);

CREATE INDEX IF NOT EXISTS ix_raw_bank_statements_ip
    ON raw_bank_statements (ip_id);
CREATE INDEX IF NOT EXISTS ix_raw_bank_statements_batch
    ON raw_bank_statements (import_batch_id);
CREATE INDEX IF NOT EXISTS ix_raw_bank_statements_account
    ON raw_bank_statements (account_id);

COMMENT ON TABLE raw_bank_statements IS
'G-BANK-2: исходный файл банковской выписки целиком (MT940-текст / xlsx-байты). Provenance для всех строк.';
COMMENT ON COLUMN raw_bank_statements.file_hash IS
'SHA-256 содержимого файла. Повторная загрузка того же файла тем же ИП и провайдером отклоняется (uq_raw_bank_statements_file).';
COMMENT ON COLUMN raw_bank_statements.account_id IS
'Счёт ERP, сопоставленный до/во время импорта. NULL = сопоставление отложено; строки при этом тоже ждут (account_id NULL).';
```

**Проверено:** все referenced таблицы/колонки существуют (`ip_entities.id`, `accounts.id`, `import_batches.id`, `users.id`).

### 1.2 `bank_statement_lines`

```sql
CREATE TABLE IF NOT EXISTS bank_statement_lines (
    id                  BIGSERIAL       PRIMARY KEY,
    ip_id               BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    statement_id        BIGINT          NOT NULL REFERENCES raw_bank_statements (id) ON DELETE RESTRICT,
    account_id          BIGINT          NULL REFERENCES accounts (id) ON DELETE RESTRICT,

    provider_code       VARCHAR(20)     NOT NULL,
    provider_account    VARCHAR(64)     NOT NULL,
    value_date          DATE            NOT NULL,
    entry_date          DATE            NULL,
    amount              NUMERIC(15,2)   NOT NULL,
    currency            VARCHAR(3)      NOT NULL DEFAULT 'KZT',
    direction           VARCHAR(3)      NOT NULL,
    document_number     VARCHAR(100)    NULL,
    bank_reference      VARCHAR(200)    NULL,

    counterparty_name   VARCHAR(255)    NULL,
    counterparty_iin    VARCHAR(12)     NULL,
    counterparty_bik    VARCHAR(20)     NULL,
    counterparty_account VARCHAR(64)    NULL,
    payment_purpose     TEXT            NULL,
    knp_code            VARCHAR(10)     NULL,

    balance_after       NUMERIC(15,2)   NULL,
    raw_line            TEXT            NOT NULL,
    line_no             INTEGER         NOT NULL,
    provider_extra      JSONB           NOT NULL DEFAULT '{}',

    line_fingerprint    CHAR(64)        NOT NULL,

    match_status        VARCHAR(20)     NOT NULL DEFAULT 'UNMATCHED',
    matched_operation_id BIGINT        NULL REFERENCES operations (id) ON DELETE RESTRICT,
    matched_at          TIMESTAMPTZ     NULL,
    matched_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_bank_statement_lines_direction
        CHECK (direction IN ('IN', 'OUT')),
    CONSTRAINT chk_bank_statement_lines_amount_positive
        CHECK (amount > 0),
    CONSTRAINT chk_bank_statement_lines_currency
        CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_bank_statement_lines_iin
        CHECK (counterparty_iin IS NULL OR counterparty_iin ~ '^[0-9]{12}$'),
    CONSTRAINT chk_bank_statement_lines_fingerprint
        CHECK (line_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_bank_statement_lines_provider
        CHECK (provider_code IN ('HALYK', 'FREEDOM', 'EPAY', 'KASPI_PAY', 'MANUAL')),
    CONSTRAINT chk_bank_statement_lines_match_status
        CHECK (match_status IN ('UNMATCHED', 'SUGGESTED', 'CONFIRMED', 'IGNORED')),
    CONSTRAINT chk_bank_statement_lines_match_consistency
        CHECK (
            (match_status IN ('SUGGESTED', 'CONFIRMED') AND matched_operation_id IS NOT NULL)
            OR (match_status IN ('UNMATCHED', 'IGNORED') AND matched_operation_id IS NULL)
        ),
    CONSTRAINT chk_bank_statement_lines_line_no
        CHECK (line_no >= 0),
    CONSTRAINT uq_bank_statement_lines_fingerprint
        UNIQUE (ip_id, line_fingerprint)
);

CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_statement
    ON bank_statement_lines (statement_id);
CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_match
    ON bank_statement_lines (ip_id, match_status);
CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_value_date
    ON bank_statement_lines (ip_id, value_date);
CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_account
    ON bank_statement_lines (account_id);

COMMENT ON TABLE bank_statement_lines IS
'G-BANK-3: нормализованные строки выписки (staging). Внешний факт; неизменяемы после импорта (триггер). Связь с ledger — только через matched_operation_id.';
COMMENT ON COLUMN bank_statement_lines.line_fingerprint IS
'SHA-256 нормализованного ядра строки (§3). Дедупликация per-IP до ledger.';
COMMENT ON COLUMN bank_statement_lines.provider_extra IS
'Provider-specific поля без нормализации + audit trail (ignore_line пишет reason сюда).';
```

### 1.3 Immutability-триггер (сильный паттерн)

По `fn_forbid_delete()`, а не по слабому DI-005 (см. предусловие 14):

```sql
CREATE OR REPLACE FUNCTION fn_bank_statement_lines_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_migrator_role TEXT := 'erp_migrator';
BEGIN
    -- Разрешён только status-workflow сверки; факты строки неизменны.
    IF TG_OP = 'UPDATE' THEN
        IF NEW.ip_id IS DISTINCT FROM OLD.ip_id
           OR NEW.statement_id IS DISTINCT FROM OLD.statement_id
           OR NEW.account_id IS DISTINCT FROM OLD.account_id
           OR NEW.provider_code IS DISTINCT FROM OLD.provider_code
           OR NEW.provider_account IS DISTINCT FROM OLD.provider_account
           OR NEW.value_date IS DISTINCT FROM OLD.value_date
           OR NEW.entry_date IS DISTINCT FROM OLD.entry_date
           OR NEW.amount IS DISTINCT FROM OLD.amount
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.direction IS DISTINCT FROM OLD.direction
           OR NEW.document_number IS DISTINCT FROM OLD.document_number
           OR NEW.bank_reference IS DISTINCT FROM OLD.bank_reference
           OR NEW.counterparty_name IS DISTINCT FROM OLD.counterparty_name
           OR NEW.counterparty_iin IS DISTINCT FROM OLD.counterparty_iin
           OR NEW.counterparty_bik IS DISTINCT FROM OLD.counterparty_bik
           OR NEW.counterparty_account IS DISTINCT FROM OLD.counterparty_account
           OR NEW.payment_purpose IS DISTINCT FROM OLD.payment_purpose
           OR NEW.knp_code IS DISTINCT FROM OLD.knp_code
           OR NEW.balance_after IS DISTINCT FROM OLD.balance_after
           OR NEW.raw_line IS DISTINCT FROM OLD.raw_line
           OR NEW.line_no IS DISTINCT FROM OLD.line_no
           OR NEW.provider_extra IS DISTINCT FROM OLD.provider_extra
           OR NEW.line_fingerprint IS DISTINCT FROM OLD.line_fingerprint
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
        THEN
            RAISE EXCEPTION
                'bank_statement_lines: statement facts are immutable. '
                'Only match_status/matched_* may change via reconciliation workflow.'
                USING ERRCODE = 'check_violation';
        END IF;
        -- match_status/matched_operation_id/matched_at/matched_by — разрешены
        RETURN NEW;
    END IF;
    -- DELETE: только через GUC + членство в erp_migrator (тестовый purge, §94)
    IF coalesce(current_setting('app.allow_financial_delete', TRUE), 'off') = 'on'
       AND coalesce((
               SELECT true FROM pg_roles r
               WHERE r.rolname = v_migrator_role
                 AND pg_has_role(current_user, r.rolname, 'member')
           ), false) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'bank_statement_lines: DELETE forbidden (§16).'
        USING ERRCODE = 'restrict_violation';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_bank_statement_lines_immutable ON bank_statement_lines;
CREATE TRIGGER trg_bank_statement_lines_immutable
    BEFORE UPDATE OR DELETE ON bank_statement_lines
    FOR EACH ROW EXECUTE FUNCTION fn_bank_statement_lines_immutable();
```

**Почему UPDATE частично разрешён** (отличие от DI-005): `match_status` — это workflow сверки, а не финансовый факт. Альтернатива (отдельная таблица статусов) усложняет без выгоды; триггер явно перечисляет запрещённые к изменению колонки.

### 1.4 SQLAlchemy-модели (parity)

```python
class RawBankStatement(Base):
    __tablename__ = "raw_bank_statements"
    __table_args__ = (
        CheckConstraint(
            "provider_code IN ('HALYK','FREEDOM','EPAY','KASPI_PAY','MANUAL')",
            name="chk_raw_bank_statements_provider"),
        CheckConstraint(
            "file_hash ~ '^[0-9a-f]{64}$'",
            name="chk_raw_bank_statements_file_hash"),
        UniqueConstraint("ip_id", "provider_code", "file_hash",
                         name="uq_raw_bank_statements_file"),
        Index("ix_raw_bank_statements_ip", "ip_id"),
        Index("ix_raw_bank_statements_batch", "import_batch_id"),
        Index("ix_raw_bank_statements_account", "account_id"),
    )
    # ... mapped_column() по DDL выше, типы: BigInteger/String(20)/Text/
    # LargeBinary/Text/Date/Numeric(15,2)/DateTime(timezone=True)


class BankStatementLine(Base):
    __tablename__ = "bank_statement_lines"
    __table_args__ = (
        # все chk_* из DDL как CheckConstraint с теми же именами,
        UniqueConstraint("ip_id", "line_fingerprint",
                         name="uq_bank_statement_lines_fingerprint"),
        Index("ix_bank_statement_lines_statement", "statement_id"),
        Index("ix_bank_statement_lines_match", "ip_id", "match_status"),
        Index("ix_bank_statement_lines_value_date", "ip_id", "value_date"),
        Index("ix_bank_statement_lines_account", "account_id"),
    )
```

**Обязательно в том же scope:** добавить `bank_code: Mapped[str | None] = mapped_column(String(20), nullable=True)` в `Account` (закрывает G-BANK-1 model drift, предусловие 4). Без этого `test_models_match_schema` упадёт на новой колонке.

**`test_indexes_declared`:** все 7 новых индексов должны быть задекларированы в `__table_args__` (имена 1-в-1 с DDL) — иначе тест упадёт. Это же касается `uq_*` unique-индексов.

---

## 2. `StatementImportService` — сигнатуры и псевдокод

Расположение: `app/services/bank_import.py` (новый модуль; не раздувать `app/services/__init__.py`).

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

@dataclass(frozen=True)
class ImportResult:
    statement_id: int
    batch_id: int
    status: str                    # SUCCESS | PARTIAL | FAILED | DRY_RUN
    rows_total: int
    rows_inserted: int
    rows_duplicates: int
    rows_skipped: int
    rows_errors: int
    errors: list[dict] = field(default_factory=list)   # cap как в ETL (50)
    balance_control: dict | None = None  # {"expected":..,"actual":..,"ok":bool}


class StatementImportService:
    def __init__(self, adapters: list[ProviderAdapter]) -> None: ...

    def import_statement(
        self,
        session: Session,
        *,
        ip_id: int,
        account_id: int | None,
        content: bytes,
        filename: str,
        column_map: Path | None = None,
        created_by: int | None = None,
        dry_run: bool = False,
        chunk_size: int | None = 1000,
    ) -> ImportResult: ...

    def _resolve_account(
        self, session: Session, *, ip_id: int,
        account_id: int | None, provider_code: str,
        provider_account: str,
    ) -> int | None:
        """Маппинг provider_code+provider_account(IBAN) → accounts.

        1. Если account_id задан явно — проверить существование и
           принадлежность ip_id (SELECT ... WHERE id=:a AND ip_id=:ip),
           иначе ValueError (не 500).
        2. Иначе поиск: accounts WHERE ip_id=:ip AND bank_code=:provider_code
           AND is_active. Точное совпадение IBAN хранится в provider_extra
           счёта? — НЕТ, IBAN в accounts не хранится (нет колонки).
           Поэтому: если ровно один активный счёт с bank_code=provider_code
           → взять его; если 0 или >1 → вернуть None (строки ждут ручного
           сопоставления; это НЕ ошибка).
        """
```

### 2.1 `import_statement()` — псевдокод

```
1. file_hash = sha256(content).hexdigest()
   batch = INSERT import_batches(
       source_type='BANK', source_name=filename, source_hash=file_hash,
       ip_id=ip_id, is_dry_run=dry_run, created_by=created_by)  # status RUNNING
   session.flush()  # batch.id нужен дальше

2. DUP-FILE: SELECT id FROM raw_bank_statements
      WHERE ip_id=:ip AND provider_code=:pc AND file_hash=:fh
   Найден → batch.status='SUCCESS', rows_duplicates=count(lines of statement),
      rows_total=тот же count, finished_at=now(); RETURN ImportResult(status SUCCESS,
      statement_id=found, все строки duplicates, 0 inserts).
   ПРИМЕЧАНИЕ: provider_code на этом шаге неизвестен до парсинга — см. шаг 3.
      Порядок: сначала лёгкий detect (can_parse без полного парсинга),
      затем dup-check, затем полный parse. can_parse обязан быть дешёвым.

3. adapter = first(a for a in adapters if a.can_parse(content, filename))
   нет → batch FAILED "no adapter for file", RETURN.

4. parsed = adapter.parse(content, filename)
   validate(parsed):
     - все tx.amount > 0, иначе rows_skipped + warning (не молча)
     - currency ~ ^[A-Z]{3}$; если не KZT → rows_skipped с причиной
       "non-KZT blocked until OQ-014" (дизайн §9.4), НЕ вставлять
     - value_date обязательна
   parse_warnings → batch.error_details["warnings"]

5. account_id = _resolve_account(...)  # может быть None

6. raw = INSERT raw_bank_statements(
       ip_id, provider_code=parsed.provider_code, account_id,
       file_name=filename, file_hash=file_hash, content=content,
       content_text=content.decode('utf-8', errors='replace') if text-format else None,
       statement_no, period_from/to, opening/closing_balance, currency,
       import_batch_id=batch.id, created_by=created_by)
   session.flush()  # raw.id

7. fps = [fingerprint(tx) for tx in parsed.transactions]
   existing = batch_select(ip_id, fps по 1000)  # P0-2
   seen = set()
   counters = {total, inserted, duplicates, skipped, errors}
   errors = []

   for tx, fp in zip(...):
       counters[total] += 1
       if fp in existing or fp in seen:
           counters[duplicates] += 1; continue
       line = BankStatementLine(... match_status='UNMATCHED', ...)
       session.add(line)
       try:
           with session.begin_nested():      # F-ETL-008 savepoint per-row
               session.flush()
       except IntegrityError as exc:
           if "uq_bank_statement_lines_fingerprint" in str(exc.orig):
               counters[duplicates] += 1     # гонка → тоже duplicates
               session.rollback()            # откат savepoint уже сделан выходом из with
               continue
           raise
       seen.add(fp); counters[inserted] += 1
       # chunked commit каждые chunk_size вставленных (P0-2), кроме dry_run

   dry_run → session.rollback(); batch.status='DRY_RUN'; RETURN (statement_id=None)

8. BALANCE-CONTROL (обязательный, дизайн §5.4):
   if parsed.opening_balance is not None and parsed.closing_balance is not None:
       expected = opening + Σ(IN) − Σ(OUT)   # по ВСЕМ строкам файла, не только новым
       ok = (expected == closing)
       if not ok:
           batch.status='FAILED'
           batch.error_details={"balance_control": {...}}
           session.commit(); RETURN FAILED
       # расхождение при повторном импорте того же файла невозможно:
       # dup-file отклонён на шаге 2, так что контроль всегда по полному файлу

9. Финализация:
   batch.rows_* = counters; batch.finished_at=now()
   batch.status = 'SUCCESS' if errors==0 else 'PARTIAL'
   raw.processed_at = now()
   session.commit()
   RETURN ImportResult(...)
```

**Ключевые инварианты:**
- Повторный импорт того же файла → 0 новых строк, `rows_duplicates = rows_total` (шаг 2).
- Повторный импорт после crash (batch остался RUNNING/FAILED) → строки уже в БД ловятся fingerprint (шаг 7), файл — нет (raw вставляется заново? НЕТ: шаг 2 проверяет `raw_bank_statements` по `file_hash` — raw уже существует → dup-file). Crash между raw-insert и commit → откатано целиком, повтор идёт с нуля. Crash после commit → dup-file. Промежуточного "половинчатого" состояния нет, кроме chunked-commit: чанки закоммичены, остаток докачивается, дубли ловятся fingerprint.
- Concurrent imports одного файла: оба проходят шаг 2 (raw ещё нет) → один выигрывает `uq_raw_bank_statements_file`, второй получает IntegrityError → трактовать как dup-file (перечитать существующий statement_id, вернуть SUCCESS/duplicates). **Обязательно**: перехват IntegrityError на шаге 6 с проверкой `"uq_raw_bank_statements_file" in str(exc.orig)`.
- Overlapping date ranges разных файлов → дедупликация только по fingerprint, не по датам. Пересечение дат — нормально.

### 2.2 Адаптеры (минимальный контракт для 0018-волны)

В scope волны: `ProviderAdapter` ABC + `registry` + `MT940StatementAdapter` (Halyk, формат VERIFIED). `ExcelStatementAdapter` — каркас с `column_map` (первый реальный файл Freedom — OD-BANK-1/B, до него парсер не тестируем на реальных данных).

```python
class ProviderAdapter(ABC):
    provider_code: str
    format_name: str

    @abstractmethod
    def can_parse(self, content: bytes, filename: str) -> bool: ...
    @abstractmethod
    def parse(self, content: bytes, filename: str) -> ParsedStatement: ...
    def fingerprint(self, tx: NormalizedBankTransaction) -> str:
        return _core_fingerprint(tx)   # §3
```

`ParsedStatement` — как в дизайне §3.1, плюс `file_hash` считает сервис (не адаптер).

---

## 3. Fingerprint spec

### 3.1 Core fingerprint (все форматы)

```python
def _core_fingerprint(tx: NormalizedBankTransaction) -> str:
    payload = {
        "provider":   tx.provider_code,
        "account":    tx.provider_account.strip(),
        "value_date": tx.value_date.isoformat(),
        "amount":     f"{tx.amount:.2f}",      # нормализация E-2
        "direction":  tx.direction,
        "document":   (tx.document_number or "").strip(),
        "bank_ref":   (tx.bank_reference or "").strip(),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

**Почему `provider` и `account` входят:** одна и та же реальная выплата в выписке Halyk (списание) и в ePay-отчёте — разные внешние факты; схлопывание — только на уровне reconciliation, не fingerprint.

**Почему `counterparty_iin` НЕ входит:** может отсутствовать в слабых форматах (Freedom Excel) — включение создало бы ложные дубликаты при дозаполнении. Контрагент участвует в matcher (§5 дизайна), не в dedup.

**Почему `entry_date` НЕ входит:** у Halyk `entry_date` может отличаться от `value_date` при перегенерации выписки за тот же период → ложные "новые" строки. `value_date` — дата валютирования, стабильна.

### 3.2 Per-format ключи

| Формат | `document_number` | `bank_reference` | Сила ключа |
|---|---|---|---|
| Halyk MT940 | `:61:` reference (6-е поле) | `:20:` + `:28C:` (transaction ref + statement no) | Сильная: нативный банковский ключ |
| Freedom Excel (column-map) | колонка документа (map) | `""` → fallback | Слабая: держится на дате+сумме+направлении; риск коллизии при двух одинаковых платежах в один день одному контрагенту — mitigated `line_no`? НЕТ, `line_no` не входит (перенумерация при переэкспорте). Остаточный риск документирован; matcher и ручная сверка — вторая линия |
| Kaspi Pay (будущее) | из выписки | — | EXTERNAL-BLOCKER: формат не подтверждён |

### 3.3 MT940 `:61:` разбор (для `MT940StatementAdapter`)

Формат `:61:`: `Value Date(6) Entry Date(4, опц.) Debit/Credit Mark(1-2) Funds Code(1) Amount(15) Transaction Type(1+3) Reference(16) Bank Reference(опц.) // Supplementary`.

- D/C mark: `D`, `EC`, `ED` → OUT; `C`, `RC` → IN. Неизвестный марк → `rows_skipped` + warning (не догадка, §94).
- Amount: MT940 использует `,` как десятичный разделитель → нормализация к `.` + 2 знака.
- `:86:`: KZ-парсинг — поиск `КНП`/`KNP` + код; БИН/ИИН — 12 цифр подряд; остальное — в `payment_purpose` verbatim + `provider_extra["mt940_86_raw"]`.

---

## 4. PostingPolicy — классификация строк

### 4.1 Принцип (из задачи, без компромиссов)

> Не превращать автоматически каждую банковскую строку в доход/расход.

`post_unmatched_as_operations()` создаёт операции **только** по явной политике; неоднозначное → `NEEDS_REVIEW` (строка остаётся `UNMATCHED`, решение — человек).

### 4.2 Таблица классификации v1

| Сигнал в строке | Предлагаемая операция | Статус | Тип операции |
|---|---|---|---|
| `knp_code` в справочнике банковских комиссий (seed) | `EXPENSE_BANK_FEE`, amount=line.amount, direction OUT | по `policy.default_status` | seed §6.2 |
| Пара OUT+IN: та же сумма (±tolerance), ±1 день, разные `provider_account`, оба счёта принадлежат одному IP (`accounts.ip_id`) | **НЕ создавать операции.** Предложить `SUGGESTED` к существующему `transfers` (матчинг по `from_account_id/to_account_id/amount/transfer_date`); если transfer нет — оставить `UNMATCHED` с пометкой `provider_extra["transfer_hint"]=true` | — | — |
| `direction=IN`, контрагент известен, есть `SUGGESTED`-матч к POSTED операции | `CONFIRMED`-матч (не новая операция) | — | — |
| Всё остальное | — | `NEEDS_REVIEW` (остаётся `UNMATCHED`) | — |

**Что намеренно НЕ авто-классифицируется в v1** (требует человека или будущих правил): SALE vs LOAN vs OWNER_WITHDRAWAL vs TAX vs PAYROLL vs REFUND. Причина: банковская строка не содержит этих сущностей; автоматический `MATCH` только по сумме запрещён (задача §6). Расширение — отдельными классифаерами с явными правилами, не эвристиками.

### 4.3 Создание операции из строки

```python
def post_unmatched_as_operations(
    session, *, ip_id: int, line_ids: list[int],
    policy: PostingPolicy, created_by: int | None = None,
) -> list[int]:
    created = []
    for line in lines:  # только match_status='UNMATCHED', тот же ip_id
        kind = classify(line, policy)   # §4.2; NEEDS_REVIEW → skip
        op_id = add_operation(session, OperationCreate(
            ip_id=ip_id,
            type_id=kind.type_id,
            direction="OUT" if line.direction=="OUT" else "IN",
            amount=±line.amount,          # знак по direction (§27)
            operation_date=line.value_date,   # факт банка (GAP-004)
            accrual_date=policy.accrual_date, # None → pure settlement (D1);
                                             # или value_date по учётной политике
            comment=(line.payment_purpose or "")[:500],
            import_fingerprint=line.line_fingerprint,  # ← дедупликация постинга
        ))
        # account_id: OperationCreate его не принимает (предусловие 17).
        # После add_operation: session.get(Operation, op_id).account_id = line.account_id
        # с проверкой fk_operations_account_ip (SELECT accounts WHERE id=:a AND ip_id=:ip).
        # Альтернатива — расширить OperationCreate (API-контракт); v1 — прямое
        # присвоение в сервисе, без изменения публичной схемы.
        op = session.get(Operation, op_id)
        op.account_id = verified_account_id   # NULL если строка без счёта
        op.import_batch_id = line.statement.import_batch_id
        line.match_status = 'CONFIRMED'       # авто-созданная операция = подтверждённый матч
        line.matched_operation_id = op_id
        line.matched_at = now(); line.matched_by = created_by
        session.flush()   # per-row savepoint как §2.1: IntegrityError на
                          # uq_operations_import_fingerprint → skip (уже запощено)
        created.append(op_id)
    return created
```

**Дедупликация постинга:** `import_fingerprint = line.line_fingerprint` + partial unique `uq_operations_import_fingerprint (ip_id, import_fingerprint)`. Повторный постинг той же строки → IntegrityError → skip. Строка при этом уже `CONFIRMED`, повторный вызов её не выберет (фильтр `UNMATCHED`).

### 4.4 `PostingPolicy`

```python
@dataclass(frozen=True)
class PostingPolicy:
    default_status: Literal["DRAFT", "POSTED"] = "DRAFT"
    # DRAFT по умолчанию: деньги по факту банка двинулись, но бухгалтер
    # подтверждает классификацию. POSTED — только явной политикой "доверяем банку".
    auto_post_commissions: bool = False   # EXPENSE_BANK_FEE сразу POSTED?
    accrual_date: date | None = None     # None → pure settlement (D1), не в P&L
    commission_knp_codes: frozenset[str] = frozenset()  # из seed-справочника
    amount_tolerance: Decimal = Decimal("1.00")
    date_window_days: int = 1
```

---

## 5. Reconciliation — matcher v1 (уточнение дизайна §5.2)

Детерминированный, без ML. Для каждой `UNMATCHED`-строки (фильтр по `ip_id`, при наличии `account_id`):

1. **EXACT** → `SUGGESTED`: `operations` POSTED, `account_id` совпадает (или оба NULL), `ABS(o.amount) = line.amount` с учётом знака (`o.amount<0 ⟺ OUT`), `o.operation_date BETWEEN value_date ± date_window_days`, `o.id NOT IN (уже замачченные)`, опционально `counterparty_iin` match. Порог — `policy`.
2. **COMMISSION** → `SUGGESTED` как *кандидат к созданию* `EXPENSE_BANK_FEE` (не матчинг к существующей операции): `knp_code ∈ commission_knp_codes`.
3. **TRANSFER_PAIR** → пометка `provider_extra["transfer_hint"]=true`, остаётся `UNMATCHED` (операции не создаются, §4.2).
4. Иначе → `UNMATCHED` (ручной разбор).

**Запрещено:** авто-`CONFIRMED` без человека, кроме операций, созданных самим `post_unmatched_as_operations` (§4.3). `MATCH` только по сумме — запрещён.

API (v1, `VIEWER`+ для чтения, `ACCOUNTANT`+ для confirm/ignore/post — как существующие finance endpoints):
- `GET /bank/statements` — список (пагинация как в проекте)
- `POST /bank/statements/import` — multipart file upload → `import_statement()`
- `GET /bank/statements/{id}/lines?match_status=`
- `POST /bank/lines/{id}/confirm` `{operation_id}`
- `POST /bank/lines/{id}/ignore` `{reason}` (reason обязателен)
- `POST /bank/lines/suggest-matches` `{statement_id?}`
- `POST /bank/lines/post` `{line_ids[], policy}`

---

## 6. Migration plan

### 6.1 Revision `20261004_0018_bank_statements`

- **revision** = `"0018_bank_statements"`, **down_revision** = `"0017_accounts_bank_code"`.
- **upgrade():**
  1. `op.execute()` DDL §1.1 (`raw_bank_statements` + индексы + comments).
  2. `op.execute()` DDL §1.2 (`bank_statement_lines` + индексы + comments).
  3. `op.execute()` функция + триггер §1.3.
  4. Seed `EXPENSE_BANK_FEE`:
     ```sql
     INSERT INTO operation_types (code, name, direction, kind, affects_pnl, is_system)
     VALUES ('EXPENSE_BANK_FEE', 'Комиссия банка', 'OUT', 'EXPENSE', TRUE, TRUE)
     ON CONFLICT (code) DO NOTHING;
     ```
     (колонки — как существующий seed §9.1: `code, name, direction, kind, affects_pnl, is_system`).
  5. Seed справочника комиссионных КНП — **отложен**: коды КНП для комиссий — факт из реальных выписок, §94 запрещает выдумывать. Таблица-справочник `bank_fee_knp_codes (knp_code VARCHAR(10) PRIMARY KEY, description TEXT)` создаётся пустой; наполнение — по первому реальному файлу.
  6. `COMMENT ON` для новых сущностей (стиль schema.sql).
- **downgrade():**
  1. `DROP TRIGGER trg_bank_statement_lines_immutable ON bank_statement_lines;`
  2. `DROP FUNCTION fn_bank_statement_lines_immutable();`
  3. `DROP TABLE bank_statement_lines; DROP TABLE raw_bank_statements; DROP TABLE bank_fee_knp_codes;`
  4. Seed-откат `EXPENSE_BANK_FEE`: `DELETE FROM operation_types WHERE code='EXPENSE_BANK_FEE' AND is_system` — **только если нет operations с этим type_id**, иначе RAISE (защита от потери ledger-ссылок).
  5. Порядок — обратный зависимостям (lines → statements).
- **schema.sql:** DDL §1.1–1.3 вносится в `database/schema.sql` (секция ARCHITECTURAL EXTENSION, после `import_batches`), seed `EXPENSE_BANK_FEE` — в §9.1 рядом с `EXPENSE_KASPI_FEE`.
- **Baseline `_FUNCTIONS`:** добавить `"fn_bank_statement_lines_immutable()"` в `alembic/versions/20260828_0000_0001_baseline_schema.py:143` (прецедент `fbbb160`), иначе `test_downgrade_function_list_matches_database` упадёт.
- **Модели:** `RawBankStatement`, `BankStatementLine` (§1.4) + `bank_code` в `Account` (закрывает G-BANK-1 drift).
- **Alembic check:** `upgrade head` → `downgrade -1` → `upgrade head` на `erp_alembic_test`; `test_alembic_baseline.py` целиком.

### 6.2 Что НЕ входит в 0018

- `ExcelStatementAdapter` с реальным маппингом Freedom (ждёт файл, OD-BANK-1/B).
- EPay webhook endpoint (DEFERRED до OD-BANK-2).
- Валютные проводки (блок до OQ-014; парсинг валютных строк — да, постинг — нет).
- Изменение `OperationCreate` (account_id — через прямое присвоение в сервисе, §4.3).

---

## 7. Test plan

### 7.1 Idempotency matrix (обязательная)

| # | Сценарий | Ожидание |
|---|---|---|
| I-1 | Тот же файл дважды подряд | 2-й импорт: `status=SUCCESS`, `rows_inserted=0`, `rows_duplicates=rows_total`, 1 запись в `raw_bank_statements` |
| I-2 | Два разных файла, пересекающиеся даты, общие строки | Общие строки — `duplicates` (по fingerprint), уникальные — `inserted` |
| I-3 | Concurrent imports одного файла (2 потока/транзакции) | Один `SUCCESS`, второй — `SUCCESS` как dup-file (перехват `uq_raw_bank_statements_file`); 0 двойных строк |
| I-4 | Crash-retry: убить после commit чанка N (симуляция — мок на середине) | Повтор: уже вставленные строки — `duplicates`, остаток — `inserted`; `raw` — dup-file если закоммичен |
| I-5 | Partial batch: файл где 1 строка битая (amount ≤ 0) | `status=PARTIAL`, битая — `skipped`+warning, остальные — `inserted`; balance-control по всем строкам файла |
| I-6 | Balance-control mismatch (подменённый closing) | `status=FAILED`, `error_details.balance_control` заполнен, строки НЕ откатываются (факт выписки сохранён), но постинг заблокирован флагом |
| I-7 | Повторный `post_unmatched_as_operations` тех же line_ids | 0 новых операций (строки уже `CONFIRMED` + `uq_operations_import_fingerprint`) |
| I-8 | Тот же экономический факт из двух провайдеров (Halyk OUT + ePay) | 2 разные строки (fingerprint включает provider), reconciliation — кандидат на ручной разбор, НЕ авто-merge |

### 7.2 Unit: MT940 parser

Синтетические MT940-документы: `:20:/:25:/:28C:/:60F:/:61:/:62F:/:86:` — debit/credit маркеры (`D`/`C`/`EC`/`ED`/`RC`), `,`-десятичные, КНП в `:86:`, БИН 12 цифр, неизвестный D/C-марк → `skipped`+warning. `can_parse` на не-MT940 → False.

### 7.3 Unit: fingerprint

- `'12500'` vs `'12500.00'` → один fingerprint (E-2 нормализация).
- Разные провайдеры, та же сумма/дата → разные fingerprints.
- `entry_date` отличается, `value_date` та же → один fingerprint.

### 7.4 Integration: service

- `import_statement` dry_run → rollback, `status=DRY_RUN`, 0 строк в БД.
- `_resolve_account`: 0 счетов → None; 1 счёт → он; >1 → None; явный чужой `account_id` → ValueError.
- Immutability-триггер: UPDATE факта → `check_violation`; UPDATE `match_status` → ок; DELETE без GUC+migrator → `restrict_violation`; DELETE с GUC+migrator → ок (§94 purge).
- `test_models_match_schema`: все новые индексы задекларированы (имена 1-в-1).
- `test_alembic_baseline`: `_FUNCTIONS` включает новую функцию; upgrade/downgrade round-trip.

### 7.5 Security (для Agent 7 parent-трека)

- Cross-IP: импорт с `ip_id` чужим → 403 на API; прямые SQL-попытки вставки чужого `ip_id` в строки — покрыть тестом изоляции (сервис всегда берёт `ip_id` из principal, не из файла).
- `content BYTEA` — без исполнения; `content_text` — только decode, без eval.
- Raw payload не содержит секретов банка сверх самой выписки; `provider_extra` — никаких токенов (ePay webhook — отдельный тракт, HMAC проверяется до парсинга).

### 7.6 Concurrency (F-ETL-008 наследие)

- Тест I-3 — два `Session` в двух потоках, барьер перед flush.
- Гонка fingerprint на уровне строк (без dup-file): два разных файла с общей строкой одновременно → один `inserted`, второй `duplicates` через savepoint-перехват.

---

## 8. Порядок работ (для implementer)

1. DDL §1.1–1.3 → `database/schema.sql` + миграция `0018` (§6.1) + `_FUNCTIONS` + модели (§1.4, включая `Account.bank_code`).
2. `NormalizedBankTransaction` + `ProviderAdapter` ABC + registry (без MT940 — сначала контракт).
3. `MT940StatementAdapter` + unit-тесты §7.2–7.3 (TDD).
4. `StatementImportService` (§2.1) + integration-тесты §7.4 + idempotency matrix §7.1 (TDD).
5. `BankReconciliationService` matcher v1 (§5) + API endpoints.
6. `post_unmatched_as_operations` + `PostingPolicy` (§4).
7. Full gate: pytest, ruff, mypy, alembic round-trip, `git diff --check`, commit.

**Явные STOP-условия (не импровизировать):** неизвестный D/C-марк MT940 → skip+warning; валюта ≠ KZT → skip с причиной; КНП справочник пуст → комиссии только `UNMATCHED`; формат Freedom без реального файла → каркас адаптера без бизнес-логики.
