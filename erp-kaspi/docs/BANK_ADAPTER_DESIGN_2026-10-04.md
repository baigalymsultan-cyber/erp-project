# Bank Adapter Infrastructure — Design (G-BANK-2..5)

**Дата:** 2026-10-04
**Статус:** design only, код не изменён.
**База:** `docs/GAP_P1_BANKS_2026-10-04.md` (research, official sources only) — её §3–§5 углублены здесь до column-level и сигнатур.
**Ограничения:** KASPI_OQ_003 действует — никаких выдуманных API-клиентов для выписок (MC-1..MC-3 NOT_FOUND).

---

## 0. Принципы (не обсуждаются дальше)

1. **Формат-ориентированный импорт, не банк-ориентированный.** Новый банк = новый `ProviderAdapter`, ядро не меняется.
2. **Нормализованный core + provider-specific fields + raw payload.** Никакого универсального интерфейса, скрывающего реальные различия банков: каждое поле, которое есть только у одного провайдера, живёт в `provider_extra JSONB` и/или именованной колонке, но core покрывает то, что нужно ledger и сверке.
3. **Файлы, не API, для выписок.** ePay — только acquiring (checkout); выписки — MT940/Excel-файлы. Адаптеры парсят файлы; API-клиент существует только для ePay invoice/payment (другой тракт, см. §8).
4. **Provenance неизменяема.** Raw-файл хранится целиком; строки выписки — staging, тоже неизменяемы после импорта (коррекции — новыми строками/связями, как `settlement_links`).
5. **Дедупликация — до ledger.** Fingerprint на уровне строки + SHA-256 файла (паттерн P0-2, см. §4).

---

## 1. NormalizedBankTransaction

Внутренняя схема ERP (не банковский контракт). Выведена из:
- SWIFT MT940 (ISO 15022, публичный стандарт; Halyk официально экспортирует MT940) — поля `:20:`, `:25:`, `:28C:`, `:60F:`, `:61:`, `:62F:`, `:64:`, `:65:`, `:86:`.
- KZ-практики: код назначения платежа **КНП** (обязателен для KZ-учёта), БИН/ИИН контрагента (12 цифр).

```python
# app/integrations/bank/normalized.py (proposal)
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

ProviderCode = Literal["HALYK", "FREEDOM", "EPAY", "KASPI_PAY", "MANUAL"]

@dataclass(frozen=True)
class NormalizedBankTransaction:
    # --- CORE: обязан выдать каждый ProviderAdapter ---
    provider_code: ProviderCode       # кто выдал строку
    provider_account: str             # IBAN / номер счёта как в выписке
    value_date: date                  # дата валютирования (MT940 :61: value date)
    entry_date: date | None           # дата проводки банком (может отличаться)
    amount: Decimal                   # всегда ПОЛОЖИТЕЛЬНАЯ, нормализована к 2 знакам
    currency: str                     # "KZT" | "USD" | "EUR" | ... (MT940 :61: 3rd char of amount field)
    direction: Literal["IN", "OUT"]   # с точки зрения счёта (MT940 :61: D/C, EC/ED)
    document_number: str | None       # номер платёжного документа (MT940 :61: reference)
    bank_reference: str | None        # уникальный референс банка (если есть; иначе см. §4)

    # --- CORE: контрагент и назначение (опционально, но нормализовано) ---
    counterparty_name: str | None = None
    counterparty_iin: str | None = None    # БИН/ИИН, 12 цифр, нормализован (только цифры)
    counterparty_bik: str | None = None   # БИК банка контрагента
    counterparty_account: str | None = None
    payment_purpose: str | None = None    # назначение платежа (MT940 :86: text)
    knp_code: str | None = None           # КНП — код назначения платежа (KZ)

    # --- CORE: сверка и provenance ---
    balance_after: Decimal | None = None  # остаток после операции (MT940 :62F:)
    raw_line: str = ""                    # исходная строка выписки, verbatim
    line_no: int = 0                      # номер строки в файле (аудит)
    source_file_hash: str = ""            # SHA-256 файла выписки

    # --- PROVIDER-SPECIFIC: не нормализуется, но сохраняется ---
    # Примеры (не исчерпывающе; каждый адаптер документирует свои ключи):
    #   HALYK MT940: {"mt940_61_raw": "...", "mt940_86_raw": "...",
    #                 "statement_no": "28C", "opening_balance": "..."}
    #   EPAY webhook: {"invoice_id": "...", "payer_card_mask": "4405**1234",
    #                  "epay_result_code": "100"}
    #   FREEDOM excel: {"sheet_row": {...}}  # исходная строка до нормализации
    provider_extra: dict = field(default_factory=dict)
```

**Почему frozen dataclass:** транзакция — факт из внешнего мира; мутировать её после парсинга запрещено. Все решения (матчинг, проводки) — отдельными сущностями.

**Что НЕ входит в core (и почему):**
- Комиссия отдельной сущностью — нет. Если банк выделяет комиссию отдельной строкой выписки, это отдельная `NormalizedBankTransaction` (direction=OUT, knp комиссии). Если комиссия «вшита» в сумму — остаётся в `provider_extra` как факт, проводка решается на этапе reconciliation (§5).
- `ip_account_id` (FK → accounts.id) — не из банка; сопоставляется на этапе `StatementImportService` по `provider_code + provider_account` → `accounts(bank_code, ...)`. G-BANK-1 (`bank_code` на accounts) уже закрыт — маппинг опирается на него.

---

## 2. Таблицы: `raw_bank_statements` + `bank_statement_lines`

### 2.1 `raw_bank_statements` (G-BANK-2)

Аналог `raw_kaspi_payloads` (§26), но generic: хранит исходный файл целиком.

```sql
CREATE TABLE IF NOT EXISTS raw_bank_statements (
    id                  BIGSERIAL       PRIMARY KEY,
    ip_id               BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    provider_code       VARCHAR(20)     NOT NULL,
    -- 'HALYK' | 'FREEDOM' | 'EPAY' | 'KASPI_PAY' | 'MANUAL'
    account_id          BIGINT          NULL REFERENCES accounts (id) ON DELETE RESTRICT,
    -- сопоставленный счёт ERP (NULL = ещё не сопоставлен, сопоставляется до строк)
    file_name           TEXT            NOT NULL,
    file_hash           CHAR(64)        NOT NULL,
    -- SHA-256 содержимого файла; повторная загрузка того же файла = duplicates, 0 новых строк
    content             BYTEA           NOT NULL,
    -- исходный файл как есть (MT940-текст, xlsx-байты). BYTEA, не TEXT: Excel — бинарный.
    content_text        TEXT            NULL,
    -- удобочитаемая копия для текстовых форматов (MT940); NULL для бинарных
    statement_no        VARCHAR(50)     NULL,
    -- номер выписки из файла (MT940 :28C:), если есть
    period_from         DATE            NULL,
    period_to           DATE            NULL,
    -- период выписки (MT940 :60F:/:62F: даты либо из имени файла)
    opening_balance     NUMERIC(15,2)   NULL,
    closing_balance     NUMERIC(15,2)   NULL,
    currency            VARCHAR(3)      NULL,
    import_batch_id     BIGINT          NULL REFERENCES import_batches (id) ON DELETE RESTRICT,
    -- связь с import_batches (source_type='BANK' уже существует в CHECK)
    received_at         TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at        TIMESTAMPTZ     NULL,
    created_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,

    CONSTRAINT chk_raw_bank_statements_provider
        CHECK (provider_code IN ('HALYK', 'FREEDOM', 'EPAY', 'KASPI_PAY', 'MANUAL')),
    CONSTRAINT chk_raw_bank_statements_file_hash
        CHECK (file_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_raw_bank_statements_file
        UNIQUE (ip_id, provider_code, file_hash)
    -- повторная загрузка того же файла тем же ИП: отклоняется/маркируется duplicates
);

CREATE INDEX IF NOT EXISTS ix_raw_bank_statements_ip
    ON raw_bank_statements (ip_id);
CREATE INDEX IF NOT EXISTS ix_raw_bank_statements_batch
    ON raw_bank_statements (import_batch_id);
```

**Отличия от `raw_kaspi_payloads` (почему не переиспользовать):**
- Kaspi-таблица хранит JSONB API-ответы с `api_type IN ('SHOP','PAY')`; банковские файлы — бинарные/текстовые, не JSON.
- Дедупликация Kaspi — по `payload_hash` ответа API; банковская — по `file_hash` всего файла + fingerprint строк.
- `request_type`/`external_id` Kaspi-семантики не имеют смысла для выписки.

### 2.2 `bank_statement_lines` (G-BANK-3)

Staging строк выписки — нормализованное представление §1, неизменяемое после импорта.

```sql
CREATE TABLE IF NOT EXISTS bank_statement_lines (
    id                  BIGSERIAL       PRIMARY KEY,
    ip_id               BIGINT          NOT NULL REFERENCES ip_entities (id) ON DELETE RESTRICT,
    statement_id        BIGINT          NOT NULL REFERENCES raw_bank_statements (id) ON DELETE RESTRICT,
    account_id          BIGINT          NULL REFERENCES accounts (id) ON DELETE RESTRICT,
    -- счёт ERP на момент импорта (денормализация для скорости сверки)

    -- CORE (§1)
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

    -- дедупликация (P0-2)
    line_fingerprint    CHAR(64)        NOT NULL,
    -- SHA-256(provider_code|provider_account|value_date|amount|direction|
    --         document_number|bank_reference) — см. §4

    -- сверка (§5)
    match_status        VARCHAR(20)     NOT NULL DEFAULT 'UNMATCHED',
    matched_operation_id BIGINT        NULL REFERENCES operations (id) ON DELETE RESTRICT,
    matched_at          TIMESTAMPTZ     NULL,
    matched_by          BIGINT          NULL REFERENCES users (id) ON DELETE SET NULL,
    -- UNMATCHED → SUGGESTED (авто) → CONFIRMED (ручное) | IGNORED

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_bank_statement_lines_direction
        CHECK (direction IN ('IN', 'OUT')),
    CONSTRAINT chk_bank_statement_lines_amount_positive
        CHECK (amount > 0),
    CONSTRAINT chk_bank_statement_lines_currency
        CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_bank_statement_lines_counterparty_iin
        CHECK (counterparty_iin IS NULL OR counterparty_iin ~ '^[0-9]{12}$'),
    CONSTRAINT chk_bank_statement_lines_fingerprint
        CHECK (line_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_bank_statement_lines_match_status
        CHECK (match_status IN ('UNMATCHED', 'SUGGESTED', 'CONFIRMED', 'IGNORED')),
    CONSTRAINT chk_bank_statement_lines_match_consistency
        CHECK (
            (match_status IN ('SUGGESTED', 'CONFIRMED') AND matched_operation_id IS NOT NULL)
            OR (match_status IN ('UNMATCHED', 'IGNORED') AND matched_operation_id IS NULL)
        ),
    CONSTRAINT uq_bank_statement_lines_fingerprint
        UNIQUE (ip_id, line_fingerprint)
    -- per-IP unique, как uq_operations_import_fingerprint (паттерн P0-2)
);

CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_statement
    ON bank_statement_lines (statement_id);
CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_match
    ON bank_statement_lines (ip_id, match_status);
CREATE INDEX IF NOT EXISTS ix_bank_statement_lines_value_date
    ON bank_statement_lines (ip_id, value_date);
```

**Почему staging отдельно от operations:**
- Строка выписки — внешний факт; операция ledger — внутреннее решение. Между ними — reconciliation (§5), который может быть ручным.
- `settlement_links` уже задаёт прецедент: «relationship assertions, not money». `matched_operation_id` — такая же assertion.
- Immutability: UPDATE/DELETE строк запрещены триггером (паттерн DI-005), кроме `match_status`/`matched_*` (статус сверки — workflow, не факт).

### 2.3 Связи с существующим

```
raw_bank_statements.import_batch_id ──→ import_batches (source_type='BANK' ✓ уже в CHECK)
bank_statement_lines.matched_operation_id ──→ operations (RESTRICT, как везде)
operations.account_id ──→ accounts (bank_code для маппинга, G-BANK-1 ✓)
operations.import_batch_id ──→ import_batches (операции, созданные из выписки)
operations.import_fingerprint ──→ CHAR(64) (операции из выписки получают fingerprint строки)
```

**`EXPENSE_BANK_FEE` (G-BANK-7):** тип операции отсутствует в seed (проверено: есть только `EXPENSE_KASPI_FEE`). Нужен seed `('EXPENSE_BANK_FEE', 'Комиссия банка', 'OUT', 'EXPENSE', ...)` миграцией — без него комиссии из выписки некуда проводить (§5.2).

---

## 3. Adapter interface

```
BankProvider (enum)
      │
      ▼
ProviderAdapter (ABC)          # один на ФОРМАТ, не на банк
  ├── MT940StatementAdapter    # Halyk Onlinebank MT940 (SWIFT, публичный стандарт)
  ├── ExcelStatementAdapter    # Freedom Business / ручная выгрузка (column-map, как etl/)
  └── EPayWebhookAdapter       # Halyk ePay webhook → "order paid" (НЕ выписка; см. §8)
      │
      ▼  list[NormalizedBankTransaction]
StatementImportService
  ├── parse → validate → deduplicate → persist (raw + lines)
      │
      ▼
ReconciliationService (§5)
      │
      ▼
Financial Operation (operations, POSTED/DRAFT по политике)
```

### 3.1 Сигнатуры

```python
# app/integrations/bank/provider.py
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BankProvider = Literal["HALYK", "FREEDOM", "EPAY", "KASPI_PAY", "MANUAL"]

@dataclass(frozen=True)
class ParsedStatement:
    """Результат парсинга одного файла — до персистентности."""
    provider_code: BankProvider
    provider_account: str            # IBAN из :25: / колонки файла
    statement_no: str | None
    period_from: date | None
    period_to: date | None
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    currency: str                    # "KZT" по умолчанию (§15: KZT only, OQ-014)
    transactions: list[NormalizedBankTransaction]
    parse_warnings: list[str]        # неблокирующие (неизвестная :86:-подстрока и т.п.)
    file_hash: str                   # SHA-256 исходного файла (считает сервис, не адаптер)


class ProviderAdapter(ABC):
    """Один адаптер = один ФОРМАТ. Банк выбирается маппингом provider→adapter."""

    provider_code: BankProvider       # какой provider обслуживает
    format_name: str                 # "MT940" | "EXCEL_STATEMENT" | "EPAY_WEBHOOK"

    @abstractmethod
    def can_parse(self, content: bytes, filename: str) -> bool:
        """Быстрая детекция формата без полного парсинга."""

    @abstractmethod
    def parse(self, content: bytes, filename: str) -> ParsedStatement:
        """Полный парсинг. §94: неизвестное — ошибка/предупреждение, не догадка."""

    def fingerprint(self, tx: NormalizedBankTransaction) -> str:
        """§4. Дефолт — core-fingerprint; адаптер может переопределить,
        если у формата есть сильный нативный ключ (напр. MT940 :61: reference)."""
        ...


class MT940StatementAdapter(ProviderAdapter):
    provider_code = "HALYK"
    format_name = "MT940"
    # Парсит :20: :25: :28C: :60F: :61: :62F: :64: :65: :86:
    # KZ-специфика: вычленение КНП из :86: ("/KNP..." или текстовая метка),
    # БИН/ИИН контрагента из :86:.


class ExcelStatementAdapter(ProviderAdapter):
    provider_code = "FREEDOM"        # первый потребитель; банк-нейтрален по сути
    format_name = "EXCEL_STATEMENT"

    def __init__(self, column_map: Path) -> None: ...
    # column-map как etl/column_map.example.yaml (OQ-002): имена колонок
    # Freedom Business неизвестны до первого реального файла.


class EPayWebhookAdapter(ProviderAdapter):
    """НЕ выписка. Обрабатывает webhook Halyk ePay (HMAC-SHA256, X-Signature)
    → событие 'order paid'. Отдельный тракт, см. §8."""
    provider_code = "EPAY"
    format_name = "EPAY_WEBHOOK"
```

```python
# app/services/bank_import.py (proposal)
class StatementImportService:
    """Файл → raw_bank_statements + bank_statement_lines. Без ledger-решений."""

    def import_statement(
        self,
        session: Session,
        *,
        ip_id: int,
        account_id: int | None,     # None = сопоставить позже (строки ждут)
        content: bytes,
        filename: str,
        column_map: Path | None = None,   # для EXCEL_STATEMENT
        created_by: int | None = None,
        dry_run: bool = False,
    ) -> ImportResult:
        """
        1. file_hash = SHA-256(content); проверка uq_raw_bank_statements_file
           → дубль файла: status SUCCESS, rows_duplicates = все строки, 0 inserts.
        2. Выбор адаптера: registry перебирает can_parse().
        3. adapter.parse() → ParsedStatement (+ validate: §94).
        4. batch = import_batches(source_type='BANK', source_hash=file_hash, ...).
        5. INSERT raw_bank_statements (content BYTEA).
        6. Для каждой транзакции: fingerprint (батч-проверка по 1000, P0-2);
           дубли → duplicates, новые → INSERT bank_statement_lines (match_status='UNMATCHED').
        7. Контроль остатков: opening + Σ(IN) − Σ(OUT) vs closing (если оба есть
           в файле) → расхождение = FAILED с явной ошибкой, не молча.
        8. batch.status = SUCCESS | PARTIAL | FAILED; counters как в import_excel.
        """
```

```python
# app/services/bank_reconciliation.py (proposal)
class BankReconciliationService:
    """bank_statement_lines → operations. Детерминированный matcher v1."""

    def suggest_matches(
        self,
        session: Session,
        *,
        ip_id: int,
        statement_id: int | None = None,
        amount_tolerance: Decimal = Decimal("1.00"),  # как ReconciliationRule
        date_window_days: int = 1,
    ) -> int:
        """Проставляет SUGGESTED + matched_operation_id. Возвращает число."""

    def confirm_match(
        self, session, *, line_id: int, operation_id: int, user_id: int
    ) -> None:
        """Ручное подтверждение: SUGGESTED|UNMATCHED → CONFIRMED."""

    def ignore_line(self, session, *, line_id: int, user_id: int, reason: str) -> None:
        """→ IGNORED (reason обязателен, пишется в provider_extra.audit)."""

    def post_unmatched_as_operations(
        self,
        session, *, ip_id: int, line_ids: list[int],
        policy: PostingPolicy,          # что создавать и в каком статусе
    ) -> list[int]:
        """Создание ledger-операций из строк (см. §5.3)."""
```

---

## 4. Дедупликация (P0-2 reuse)

Два уровня, оба обязательные:

### Уровень 1 — файл: `raw_bank_statements.file_hash`
SHA-256 всего содержимого. `uq_raw_bank_statements_file (ip_id, provider_code, file_hash)` — повторная загрузка отклоняется на входе. Это дешевле и строже, чем построчная проверка.

### Уровень 2 — строка: `bank_statement_lines.line_fingerprint`
Переиспользует `etl/import_excel.row_fingerprint` (canonical JSON + нормализация сумм к 2 знакам):

```
fingerprint_input = {
    "provider":      tx.provider_code,        # HALYK ≠ FREEDOM: одна и та же выплата
    "account":       tx.provider_account,     # через разные банки — разные строки
    "value_date":    tx.value_date.isoformat(),
    "amount":        f"{tx.amount:.2f}",       # нормализация (E-2)
    "direction":     tx.direction,
    "document":      tx.document_number or "",
    "bank_ref":      tx.bank_reference or "",
}
line_fingerprint = SHA-256(json.dumps(input, sort_keys=True))
```

**Что формирует fingerprint по банкам:**

| Банк / формат | document_number | bank_reference | Комментарий |
|---|---|---|---|
| Halyk MT940 | MT940 `:61:` reference (6-я подстрока) | `:20:` transaction reference + `:28C:` statement no | Сильный нативный ключ; два поля покрывают перенумерацию выписок |
| Freedom Excel | номер документа из колонки (map) | пусто → fallback | Слабый: дедупликация держится на дате+сумме+контрагенте; колонка `counterparty_iin` в fingerprint НЕ входит (может отсутствовать) — но входит в matcher §5 |
| ePay webhook | `invoice_id` | `payment_id` из webhook | Тракт checkout, не выписки |
| Kaspi Pay (будущее) | из выписки Kaspi Pay | — | MISSING-CAPABILITY: формат выписки Kaspi Pay не подтверждён |

**Проверка — батчинг по 1000** (урок P0-2, `etl/import_excel.py:255-270`):
```python
for i in range(0, len(fp_list), 1000):
    existing.update(session.scalars(
        select(BankStatementLine.line_fingerprint).where(
            BankStatementLine.ip_id == ip_id,
            BankStatementLine.line_fingerprint.in_(fp_list[i:i+1000]))))
```
Плюс per-row flush с перехватом `IntegrityError` на `uq_bank_statement_lines_fingerprint` (паттерн F-ETL-008) — гонка двух параллельных импортов превращается в `duplicates`, а не в ошибку.

**Почему `provider` и `account` входят в fingerprint:** одна и та же реальная выплата может отразиться в выписке Halyk (списание) и в ePay-отчёте — это разные факты из разных источников; схлопывать их на уровне fingerprint нельзя, только на уровне reconciliation (§5).

---

## 5. Reconciliation: generic vs `v_kaspi_reconciliation`

### 5.1 Чем они отличаются

| | `v_kaspi_reconciliation` (существует) | Bank reconciliation (этот дизайн) |
|---|---|---|
| Треугольник | Order ↔ Payout ↔ Operation | StatementLine ↔ Operation |
| Зерно | заказ/выплата (бизнес-сущности Kaspi) | банковская строка (внешний факт) |
| Вердикт | OK / AMOUNT_MISMATCH / ... (бизнес-логика Kaspi: комиссии, доставка, возвраты) | UNMATCHED / SUGGESTED / CONFIRMED / IGNORED (статус workflow) |
| Комиссии | `commission_amount` — поле payout_item | отдельная строка выписки → `EXPENSE_BANK_FEE` |
| Автопроводка | `reconcile_payout_full()` (8-class classifier) | `post_unmatched_as_operations()` по политике (§5.3) |

**Вывод:** Kaspi-сверка — про *бизнес-семантику маркетплейса* (заказ стоил X, Kaspi удержал Y). Bank reconciliation — про *факт движения денег* (банк говорит: со счёта ушло X). Это разные слои; generic-движок не заменяет Kaspi-движок, а дополняет. Точка стыковки: операция, созданная из банковской строки, может ссылаться на Kaspi-выплату через `operations.related_*` / `settlement_links`.

### 5.2 Matcher v1 (детерминированный, без ML)

Для каждой `UNMATCHED`-строки (в пределах IP и счёта):

1. **Точное совпадение** → `SUGGESTED`:
   `operations.amount = ±line.amount` (знак по direction) AND
   `operations.operation_date BETWEEN value_date ± date_window_days` AND
   `operations.account_id = line.account_id` AND
   (`operations.counterparty` ~ `line.counterparty_iin` OR нормализованное имя)
   AND `operations.status = 'POSTED'`, `operations.id` ещё не замаччен.
2. **Комиссии банка** → `SUGGESTED` как `EXPENSE_BANK_FEE`:
   строка с `knp_code` из справочника комиссионных КНП (справочник — seed/migration, пополняемый) → предлагается создать операцию `EXPENSE_BANK_FEE` (не матчить к существующей).
3. **Переводы между своими счетами:** пара строк OUT/IN на одну сумму ±1 день на разных `provider_account` одного IP → `SUGGESTED` как пара к `transfers` (существующая сущность, не трогаем).
4. Остальное → остаётся `UNMATCHED` для ручного разбора (тот же UX-паттерн, что Kaspi-сверка).

**Пороги — configurable** (`amount_tolerance`, `date_window_days`), дефолт = самый безопасный (1.00 ₸, ±1 день) — паттерн `ReconciliationRule` (§44) и OD-BANK-3/A.

### 5.3 Проводка в ledger

`post_unmatched_as_operations()` создаёт `operations` из строк по политике:

```python
@dataclass(frozen=True)
class PostingPolicy:
    default_status: Literal["DRAFT", "POSTED"] = "DRAFT"
    # DRAFT default: деньги уже ушли/пришли по факту банка, но бухгалтер
    # подтверждает классификацию. POSTED — только при политике "доверяем банку".
    auto_post_exact_matches: bool = False   # OD-BANK-3
    commission_operation_type: str = "EXPENSE_BANK_FEE"
    income_operation_type: str = ...         # configurable; дефолт требует ручного выбора
    expense_operation_type: str = ...        # configurable
```

Каждая созданная операция получает:
- `import_batch_id` = batch выписки, `import_fingerprint` = `line_fingerprint` (дедупликация на уровне операций — повторный постинг той же строки невозможен),
- `account_id` = счёт, `operation_date` = `value_date` (дата банка — факт; `accrual_date` — по учётной политике, см. GAP-004),
- `raw_payload_id` → расширение: `raw_bank_statements.id` (сейчас FK на kaspi-таблицу; нужна generic-ссылка либо отдельная колонка `raw_bank_statement_id` — решение при реализации),
- `comment` = `payment_purpose` (назначение платежа — в комментарий, не терять).

### 5.4 Контроль остатков (обязательный)

Если файл содержит `opening_balance` и `closing_balance` (MT940 `:60F:`/`:62F:`):
`opening + Σ(IN) − Σ(OUT) == closing` с точностью до копейки. Расхождение → `import_batch.status = 'FAILED'` с явной ошибкой в `error_details` («balance control failed: expected X, got Y»). Молчаливый пропуск запрещён — это финансовое утверждение о полноте.

---

## 6. Per-bank reality table

| | **Kaspi Pay** | **Halyk** | **Freedom** |
|---|---|---|---|
| Выписка: источник | Приложение Kaspi Pay / Kaspi Гид | Halyk Onlinebank (интернет-банк бизнеса) | Freedom Business (приложение) |
| Выписка: формат | NOT_FOUND (официально не документирован) | **MT940 / MT100 / XLS / PDF / 1C** — VERIFIED (офиц. PDF Halyk) | NOT_FOUND (форматы не найдены в официальных источниках) |
| API для выписок | NOT_FOUND | NOT_FOUND (MC-1) | NOT_FOUND (MC-1) |
| API для платежей | NOT_FOUND (публичного нет) | **ePay acquiring** — VERIFIED (epayment.kz): `POST /invoice`, `GET /invoice/{id}`, `POST /payment/cryptopay`, OAuth2 client_credentials, HMAC-SHA256 webhooks, sandbox `testepay.homebank.kz` | NOT_FOUND (MC-4) |
| Комиссии | Kaspi: «списывается автоматически после поступления денег; информация — в выписке Kaspi Pay» (официальная формулировка) | В выписке MT940 отдельной строкой / в составе | NOT_FOUND |
| RRN / референс | — | MT940 `:61:` reference | NOT_FOUND |
| КНП | — | В `:86:` выписки | Ожидается в выписке (KZ-обязательно) |
| Что реализуем сейчас | Ничего (EXTERNAL-BLOCKER до формата) | **MT940StatementAdapter** (формат подтверждён) | **ExcelStatementAdapter** с column-map (ждём первый реальный файл от owner — OD-BANK-1/B) |
| Что DEFERRED | Kaspi Pay statement import | API-клиент для выписок (нет контракта); исходящие платежи (MC-2) | Любой API-клиент (нет контракта) |

**Архитектурная цепочка (по требованию, без смешивания сущностей):**

```
"Kaspi Sale" (orders, KASPI_SHOP)
      │  sale event
      ▼
"Kaspi Settlement" (kaspi_payouts/payout_items — деньги от Kaspi)
      │  payout net
      ▼
"Commission" (EXPENSE_KASPI_FEE — удержано Kaspi; KASPI-003: проводить из payout)
      │
      ▼
"Bank/Kaspi account" (accounts: bank_code=KASPI|HALYK|FREEDOM — деньги ЛЕЖАТ здесь)
      │  bank statement line (внешний факт)
      ▼
"Reconciliation" (bank_statement_lines.match_status — ФАКТ банка vs УЧЁТ)
      │
      ▼
"Financial Operation" (operations — учётное решение, POSTED/DRAFT)
```

Каждая стрелка — отдельная сущность/связь. Выписка не «является» операцией; сверка не «является» проводкой.

---

## 7. Порядок реализации (с зависимостями)

| # | Шаг | Зависит от | Категория | Статус |
|---|---|---|---|---|
| 1 | `EXPENSE_BANK_FEE` seed (миграция) — G-BANK-7 | — | A | не начат |
| 2 | `raw_bank_statements` + `bank_statement_lines` (миграция 0018, schema.sql, model parity, immutability-триггер для строк по паттерну DI-005) | — | A | не начат |
| 3 | `NormalizedBankTransaction` + `ProviderAdapter` ABC + registry | — | A | не начат |
| 4 | `MT940StatementAdapter` (парсер `:20:/:25:/:28C:/:60F:/:61:/:62F:/:86:`, КНП/БИН из `:86:`) + unit-тесты на синтетических MT940 | 3 | A | не начат |
| 5 | `StatementImportService` (file_hash → parse → validate → batch-fingerprint → persist → balance-control) + `import_batches(source_type='BANK')` | 2, 3, 4 | A | не начат |
| 6 | `ExcelStatementAdapter` на column-map (переиспользование `etl/` механизма) | 3 | A | не начат (ждёт пример файла Freedom — OD-BANK-1/B) |
| 7 | `BankReconciliationService`: matcher v1 (§5.2) + `SUGGESTED/CONFIRMED/IGNORED` workflow + API endpoints | 2, 5 | B (пороги configurable) | не начат |
| 8 | `post_unmatched_as_operations()` + `PostingPolicy` | 7 | B | не начат |
| 9 | `v_bank_reconciliation` (view: line ↔ operation, verdict) — аналог `v_kaspi_reconciliation` для UI | 2 | A | не начат |
| 10 | EPayWebhookAdapter + приёмник webhook (HMAC-SHA256) — отдельный checkout-тракт | 3 | B (включение — REQUIRES_REAL_ACCOUNT) | DEFERRED до OD-BANK-2 |
| 11 | Kaspi Pay statement import | формат выписки | — | EXTERNAL-BLOCKER |

**Что намеренно НЕ в scope:** API-клиенты для выписок Halyk/Freedom (нет контракта, MC-1); исходящие платежи (MC-2); payouts API (MC-3); stock ledger (отдельный production-трек).

---

## 8. Связь с Kaspi- и ETL-треками

- **KASPI-002/003:** payout items и комиссии Kaspi — тот же паттерн «внешний факт → сверка → операция», но источник — Kaspi Seller API / выписка Kaspi Pay, не bank adapter. Точка стыковки — `operations` (комиссия Kaspi `EXPENSE_KASPI_FEE` vs комиссия банка `EXPENSE_BANK_FEE` — разные типы, не смешивать).
- **F-ETL-008:** per-row flush + `IntegrityError`-перехват на unique fingerprint — переиспользуется в `StatementImportService` (§4).
- **F-ETL-002:** bank statement import — этот документ и есть его дизайн.
- **GAP-004 (date semantics):** `operation_date` банковской операции = `value_date` (факт банка); `accrual_date` — по учётной политике; `entry_date` сохраняется в строке, но в ledger не идёт (аудит). Окончательное решение — в GAP-004 треке.

---

## 9. Риски и открытые вопросы

1. **Freedom без примера файла** — Excel-маппинг строится вслепую; mitigated column-map механизмом (OQ-002), но первый реальный файл обязателен до production-использования.
2. **MT940-диалекты Halyk** — `:86:` структурирован по-своему; парсер КНП/БИН пишется по первому реальному файлу, до этого — `provider_extra` + warnings (не молчаливый пропуск).
3. **`raw_payload_id` на operations** сейчас ссылается на Kaspi-таблицу — при реализации §5.3 решить: generic-ссылка или отдельная колонка `raw_bank_statement_id`.
4. **Мультивалютность** — §15: KZT only (OQ-014). Валютные строки выписки: парсить, сохранять, но постинг блокировать явной ошибкой до решения OQ-014.
