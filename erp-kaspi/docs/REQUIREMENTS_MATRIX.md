# REQUIREMENTS MATRIX (§2, §122)

Источник: MASTER PROMPT v3.0. Каждое требование имеет ID, шаг, реализацию, файл, тест, статус.

Статусы (§2): `PENDING` `IN_PROGRESS` `IMPLEMENTED` `TESTED` `DEFERRED` `BLOCKED` `CONFLICT`
Уровни источника (§6): **A** — исходное ТЗ, **B** — production-архитектура, **C** — инженерные практики.

---

## ЧАСТЬ I — ТРЕБОВАНИЯ ШАГА 1 (DATABASE)

### I.1 Общие правила БД

| ID | Lvl | Требование | § | Реализация | Файл | Тест | Статус |
|---|---|---|---|---|---|---|---|
| REQ-0001 | A | PostgreSQL 15+ | 13 | Проверено на 18.6; DDL без версионно-специфичных хаков | database/schema.sql | audit_step1.py::server_version | TESTED |
| REQ-0002 | A | Деньги = NUMERIC(15,2) | 13 | Все денежные колонки | database/schema.sql | check_money_types | TESTED |
| REQ-0003 | A | Количество = NUMERIC(15,3) | 13 | quantity-колонки | database/schema.sql | check_quantity_types | TESTED |
| REQ-0004 | A | Timestamp = TIMESTAMPTZ | 13 | Все временные колонки | database/schema.sql | check_timestamptz | TESTED |
| REQ-0005 | A | Raw payload = JSONB | 13, 26 | raw_kaspi_payloads.payload, audit_log.old_data/new_data | database/schema.sql | check_jsonb | TESTED |
| REQ-0006 | A | Encrypted binary = BYTEA | 13, 18 | kaspi_api_credentials.encrypted_token | database/schema.sql | check_bytea | TESTED |
| REQ-0007 | A | Хранение timestamps в UTC, UI Asia/Almaty | 14 | TIMESTAMPTZ + `SET timezone='UTC'`; конвертация в UI | database/schema.sql | check_timestamptz | TESTED |
| REQ-0008 | A | Валюта KZT, формат `1 234 567,00 ₸` | 15 | Комментарий-контракт в схеме; форматирование — UI-слой | database/schema.sql | — | IMPLEMENTED |
| REQ-0009 | A | Запрет физического DELETE финансовых данных; reversal/correction/status/audit | 16 | Триггер `trg_forbid_delete` на operations/transfers/payroll_payments/kaspi_payouts | database/schema.sql | test_forbid_delete | TESTED |
| REQ-0010 | B | Автообновление updated_at | 56 | Функция `fn_set_updated_at()` + триггеры на всех таблицах с updated_at | database/schema.sql | test_updated_at_trigger | TESTED |
| REQ-0011 | B | Идемпотентный seed | 87 | `INSERT ... ON CONFLICT DO NOTHING` по натуральным ключам | database/schema.sql | test_seed_idempotent | TESTED |
| REQ-0012 | A | Порядок DDL: extensions → functions → base → dependent → constraints → indexes → triggers → views → seed | 84 | Секции 1–10 файла | database/schema.sql | apply twice | TESTED |
| REQ-0013 | A | DDL содержит PK/FK/CHECK/UNIQUE/NOT NULL/DEFAULT/GENERATED/INDEX/TRIGGER/EXCLUDE/VIEW/SEED | 83 | Все элементы присутствуют | database/schema.sql | audit_step1.py (все проверки) | TESTED |
| REQ-0014 | B | Защита от double payment / duplicate payout / duplicate import | 57 | UNIQUE-констрейнты + partial unique indexes; транзакции/FOR UPDATE — на уровне сервисов | database/schema.sql | test_no_duplicate_* | TESTED |
| REQ-0015 | A | Никаких TODO/заглушек/сокращений в коде | 89 | Полный DDL без пропусков | database/schema.sql | grep-проверка | TESTED |

### I.2 Исходные 17 таблиц (§80, §81 — сохранить точно)

| ID | Lvl | Таблица | § | Ключевые требования | Тест | Статус |
|---|---|---|---|---|---|---|
| REQ-0101 | A | `ip_entities` | 17 | id BIGSERIAL PK; name NOT NULL; bin UNIQUE NOT NULL; CHECK length(bin)=12; is_active DEFAULT TRUE; created_at DEFAULT now; closed_at NULL | audit T01 | TESTED |
| REQ-0102 | A | `kaspi_api_credentials` | 18 | ip_id FK; api_type SHOP\|PAY; encrypted_token BYTEA; expires_at; status ACTIVE\|EXPIRED\|ERROR\|DISABLED; токен не в plaintext | audit T02 | TESTED |
| REQ-0103 | A | `operation_types` | 20 | 7 обязательных кодов; направление операции | audit T03 | TESTED |
| REQ-0104 | A | `categories` | 21 | Базовые категории доходов и расходов | audit T04 | TESTED |
| REQ-0105 | A | `counterparties` | 22 | id, name, bin_iin, phone, comment, created_at | audit T05 | TESTED |
| REQ-0106 | A | `orders` | 23 | все поля; статусы NEW..RETURNED; UNIQUE(ip_id, external_order_id) | audit T06 | TESTED |
| REQ-0107 | A | `raw_kaspi_payloads` | 26 | ip_id, request_type, received_at, payload JSONB | audit T07 | TESTED |
| REQ-0108 | A | `operations` | 27 | все поля; amount NUMERIC(15,2); CHECK amount != 0; направление из operation_type | audit T08 | TESTED |
| REQ-0109 | A | `transfers` | 28 | from/to ip; CHECK from<>to; ссылки на outbound/inbound операции | audit T09 | TESTED |
| REQ-0110 | A | `sync_log` | 45 | все поля счётчиков и ошибок | audit T10 | TESTED |
| REQ-0111 | A | `employees` | 29 | статусы ACTIVE\|ON_LEAVE\|FIRED; payment_type PIECEWORK\|SALARY\|MIXED | audit T11 | TESTED |
| REQ-0112 | A | `work_operation_types` | 30 | 11 типов работ, единицы, схема расчёта | audit T12 | TESTED |
| REQ-0113 | A | `rates_history` | 31 | ставка, effective_from/to; запрет пересечения периодов | audit T13 | TESTED |
| REQ-0114 | A | `work_entries` | 33 | quantity NUMERIC(15,3); rate_at_time NUMERIC(15,2); total_sum GENERATED STORED; статусы DRAFT\|CONFIRMED\|PAID | audit T14 | TESTED |
| REQ-0115 | A | `payroll_periods` | 34 | статусы OPEN\|PROCESSING\|CLOSED\|PAID; CHECK end>=start | audit T15 | TESTED |
| REQ-0116 | A | `payroll_summary` | 35 | piecework_total, base_salary, bonuses, deductions, total_calculated | audit T16 | TESTED |
| REQ-0117 | A | `payroll_payments` | 36 | payment_method KASPI_TRANSFER\|CASH; связь с operation | audit T17 | TESTED |

### I.3 Бизнес-логика уровня БД

| ID | Lvl | Требование | § | Реализация | Тест | Статус |
|---|---|---|---|---|---|---|
| REQ-0201 | A | 7 обязательных operation types | 20 | seed operation_types | audit seeds | TESTED |
| REQ-0202 | A | Kaspi Shop ≠ поступление в момент заказа; поток order→production→payout→payment→operations | 24 | Отсутствие авто-операции при заказе; payout-таблицы; FK operations.related_order_id | test_shop_flow (ШАГ 4) | IMPLEMENTED |
| REQ-0203 | A | Kaspi Pay → SALALE_KASPI_PAY, atomic/idempotent/traceable | 25 | partial UNIQUE(ip_id,type_id,external_id); raw_payload_id | test_kaspi_pay_idempotent (ШАГ 4) | IMPLEMENTED |
| REQ-0204 | A | Перевод = TRANSFER_OUT + TRANSFER_IN, не доход/расход | 28, 76 | `affects_pnl=FALSE`; paired ledger operations исключены из P&L | `test_transfer_excluded_from_pnl`; `test_transfer_creates_linked_neutral_operation_legs` | TESTED |
| REQ-0205 | A | 11 стартовых ставок, effective_from 2024-01-01 UTC | 30, 86 | seed rates_history | audit seeds | TESTED |
| REQ-0206 | A | Запрет пересечения периодов ставок одного типа | 31 | `EXCLUDE USING gist` + btree_gist | test_rate_overlap | TESTED |
| REQ-0207 | A | Rate freeze: rate_at_time копируется, изменение справочника не меняет старые записи | 32 | Триггер `trg_work_entry_freeze_rate` (BEFORE INSERT) | test_rate_freeze | TESTED |
| REQ-0208 | A | Material price freeze: price_at_time | 38 | Триггер `trg_order_material_freeze_price` | test_price_freeze | TESTED |
| REQ-0209 | A | total_sum = quantity * rate_at_time (GENERATED STORED) | 33 | GENERATED колонка | test_generated_total | TESTED |
| REQ-0210 | A | total_calculated = piecework + base + bonuses − deductions | 35 | GENERATED колонка | test_payroll_total | TESTED |
| REQ-0211 | A | Историческая цена в order_items | 47 | unit_price/discount/total_amount в позиции | audit T-ext | TESTED |
| REQ-0212 | A | История статусов заказа | 50 | `order_status_history` + триггер автозаписи | test_status_history | TESTED |
| REQ-0213 | A | Обязательные 4 индекса | 64 | idx_operations_analytics, idx_work_entries_emp, idx_orders_status, idx_rates_lookup | audit indexes | TESTED |
| REQ-0214 | A | Обязательные 3 view | 63 | v_ip_financial_summary, v_payroll_due, v_work_entries_detail | audit views | TESTED |
| REQ-0215 | A | Seed: operation_types, categories, work_operation_types, rates_history | 85 | Секция 10 DDL | audit seeds | TESTED |
| REQ-0216 | A | Роли: ADMIN, DIRECTOR, ACCOUNTANT, HR, SHOP_MANAGER, BRIGADIER, VIEWER | 52 | seed roles | audit seeds | TESTED |
| REQ-0217 | A | Audit log: user, entity, entity_id, action, old_data, new_data, created_at | 54 | `audit_log` + generic-триггер | test_audit_log | TESTED |
| REQ-0218 | A | Пароли — только secure hash, не plaintext | 55 | users.password_hash + CHECK на формат bcrypt/argon2 | test_no_plaintext_password | TESTED |
| REQ-0219 | A | Налоговые параметры — не константы в коде | 75 | `tax_rates_history` (EXTENSION) | audit T-ext | IMPLEMENTED |
| REQ-0220 | A | Счета (accounts) при разделении | 51 | `accounts` (EXTENSION), типы KASPI_PAY\|KASPI_SHOP\|BANK\|CASH\|OTHER | audit T-ext | TESTED |
| REQ-0221 | A | Payout/reconciliation структуры | 44 | `kaspi_payouts`, `kaspi_payout_items` (EXTENSION) | audit T-ext | TESTED |
| REQ-0222 | A | Products catalog | 46 | `products` (EXTENSION) | audit T-ext | TESTED |
| REQ-0223 | A | Materials | 48 | `materials` (EXTENSION) | audit T-ext | TESTED |
| REQ-0224 | A | Order materials | 49 | `order_materials` (EXTENSION) | audit T-ext | TESTED |
| REQ-0225 | A | Order items | 47 | `order_items` (EXTENSION) | audit T-ext | TESTED |
| REQ-0226 | A | Users/roles/user_roles | 52 | 3 таблицы (EXTENSION) | audit T-ext | TESTED |
| REQ-0227 | B | Хранение ключа шифрования вне БД | 19 | В БД только `key_version`, ключ — env | code review | IMPLEMENTED |
| REQ-0228 | B | Идемпотентность импорта через fingerprint | 41 | `import_batches` + `operations.import_fingerprint` UNIQUE | test_import_idempotent (ШАГ 2) | IMPLEMENTED |
| REQ-0229 | A | Cross-IP transfer атомарно создаёт `Transfer` и две равные противоположные POSTED legs с provenance/audit/linkage | 24, 28, 50, 54 | `add_transfer()` в request transaction; deferred pair triggers, composite FKs и `related_operation_id` | `test_transfer_creates_linked_neutral_operation_legs`; `test_transfer_failure_on_second_leg_rolls_back_all_rows`; `test_database_rejects_malformed_posted_transfer_pair`; `test_posted_transfer_leg_must_belong_to_one_transfer` | TESTED |
| REQ-0230 | A | Повтор transfer безопасен: тот же key/payload возвращает тот же результат; изменённый payload — 409; concurrency не создаёт дубль | 51, 52 | Unique `transfers.idempotency_key` + savepoint/conflict handling | `test_transfer_idempotency_replay_and_payload_conflict`; `test_transfer_concurrent_replay_creates_only_one_pair` | TESTED |
| REQ-0231 | A | Cross-IP transfer проверяет обе IP и ownership/active состояние source/destination accounts server-side | 36, 52 | `ensure_ip_access()` + validated composite account/IP FKs + service validation | `test_transfer_denies_cross_ip_idor_even_on_replay`; `test_transfer_rejects_account_owned_by_another_ip`; `test_transfer_rejects_inactive_source_or_destination_account[source]`; `test_transfer_rejects_inactive_source_or_destination_account[destination]`; `test_transfer_database_rejects_account_for_wrong_ip` | TESTED |
| REQ-0232 | A | Новый POSTED transfer не может быть непарным, несогласованным или изменён/reverse одной ногой | 7, 16, 24 | validated `chk_transfers_posted_has_legs`; deferred cross-row triggers; immutable POSTED rows | `test_transfer_posted_legs_constraint_is_validated`; `test_database_rejects_malformed_posted_transfer_pair`; `test_posted_transfer_leg_must_belong_to_one_transfer`; `test_posted_transfer_legs_cannot_be_reversed_singly_or_mutated` | TESTED |
| FIN-OPS-0001 | A | POSTED не-transfer операция не может быть тихо изменена: amount/type/ip/account/category/counterparty/date/external_id/related_*/import_fingerprint/created_by/created_at неизменяемы после DRAFT; переходы статуса только DRAFT→POSTED и POSTED→REVERSED | FINANCE-OPS-001 §7, §65 п.12 | `fn_protect_posted_ledger_operation()` + `trg_operations_protect_posted_ledger` (schema.sql, миграция 0007) | database/schema.sql, alembic/versions/20260926_1500_0007_posted_ledger_immutability.py | tests/test_ledger_immutability.py | PARTIALLY VERIFIED (2026-10-03): gate прогнан на PostgreSQL 18.6 (self-audit 656/656, alembic head 0009, pytest 495 passed) — проект требует PG15, итоговая совместимость на PG15 не проверена |

---

## ЧАСТЬ II — ТРЕБОВАНИЯ ПОСЛЕДУЮЩИХ ШАГОВ

---

## ЧАСТЬ II — ТРЕБОВАНИЯ ПОСЛЕДУЮЩИХ ШАГОВ

| ID | Lvl | Требование | § | Шаг | Статус |
|---|---|---|---|---|---|
| REQ-0301 | A | Excel ETL на Pandas: sheets, dates, numbers, empty cells, duplicates, invalid rows, validation, errors | 39 | ШАГ 2 | IMPLEMENTED — `etl/import_excel.py`; тесты `tests/test_etl.py` |
| REQ-0302 | A | `--dry-run` не изменяет БД; отчёт inserted/skipped/duplicates/errors/unknown | 40 | ШАГ 2 | IMPLEMENTED — `--dry-run` с rollback (§38), `tests/test_etl.py` |
| REQ-0303 | A | Идемпотентность повторного импорта | 41 | ШАГ 2 | IMPLEMENTED — `import_batches` + fingerprint, `tests/test_etl.py` |
| REQ-0304 | A | KaspiShopClient / KaspiPayClient раздельно, без смешения бизнес-логики | 42 | ШАГ 3 | BLOCKED (OQ-003) |
| REQ-0305 | A | Проверить официальную документацию Kaspi, не выдумывать API | 42, 94 | ШАГ 3 | BLOCKED (OQ-003) |
| REQ-0306 | A | Tenacity retry, exp backoff, timeout, rate limiting; retry не создаёт повторную фин. операцию | 43 | ШАГ 3 | IMPLEMENTED — `app/integrations/kaspi/transport.py` (RetryingTransport), `tests/test_kaspi_clients.py` |
| REQ-0307 | A | Reconciliation: duplicate/missing/amount/commission/delivery mismatch, refund, partial payout, unexplained payment | 44 | ШАГ 4 | TESTED — `reconcile_payout_full` (app/services), POST /kaspi/reconcile/{payout_id}; tests `tests/test_reconciliation.py` (13) |
| REQ-0308 | A | Direct Cost = Material + Piecework Labor; Material/Labor/Total/Revenue/Gross Profit/Margin по заказу | 37, 76 | ШАГ 6 | IMPLEMENTED — SQL view `v_order_costing` → API `/reports/order-costing` (app/api/__init__.py:1034); `order_direct_cost`/`order_gross_profit` в `app/services/__init__.py` — dead code, 0 callers |
| REQ-0309 | A | Payroll расчёты и выплаты → operations | 34–36 | ШАГ 7 | IMPLEMENTED — calculate/approve/pay + guard двойной выплаты; `tests/test_payroll_flow.py` |
| REQ-0310 | A | RBAC проверяется на backend, не только в UI | 53 | ШАГ 8 | IMPLEMENTED — `require_privilege` на всех endpoint'ах; тесты 401/403/200 |
| REQ-0311 | A | Authentication: secure hash, session/token security, input validation, least privilege | 55 | ШАГ 8 | IMPLEMENTED — bcrypt (`app/core/security.py`), JWT (`app/core/auth.py`), rate limit; `tests/test_auth_api.py` |
| REQ-0312 | A | Транзакции, SELECT FOR UPDATE, row locks против двойных выплат | 57 | ШАГ 5–7 | IMPLEMENTED — `pay_payroll_summary` (SELECT FOR UPDATE); `tests/test_payroll_flow.py` |
| REQ-0313 | A | Streamlit UI, RU/KK, KZT | 58 | ШАГ 10 | IMPLEMENTED — `frontend/app.py`; `tests/test_streamlit_ui.py` |
| REQ-0314 | A | Dashboard: оборот, выручка, расходы, чистая прибыль, зарплата, себестоимость + фильтры периодов | 59 | ШАГ 10 | NOT IMPLEMENTED — no dashboard module exists in frontend/app.py (2026-10-03 gap audit: GAP-1, P1). Individual reports exist; the §31 management dashboard with drill-down does not. |
| REQ-0315 | A | Sidebar 10 модулей | 60 | ШАГ 10 | IMPLEMENTED — 10 модулей; AppTest smoke `tests/test_streamlit_ui.py` |
| REQ-0316 | A | UI бригадира: Employee→WorkType→Order→Quantity→Date→Save, ставка автоматически | 61 | ШАГ 10 | IMPLEMENTED — foreman-форма; `tests/test_foreman_ui.py` |
| REQ-0317 | A | 8 отчётов + экспорт Excel | 62 | ШАГ 11 | PARTIALLY IMPLEMENTED — `frontend/reports.py` (7 отчётов + Excel-экспорт; 8-й отчёт не определён — BLOCKED, требуется решение владельца) |
| REQ-0318 | B | Alembic: изменения схемы только миграциями | 65 | ШАГ 1→2 | TESTED — `alembic/versions/20260828_0000_0001_baseline_schema.py`; тесты `tests/test_alembic_baseline.py` (32), `tests/test_models_match_schema.py` (81), `tests/test_core_database.py` (36) |
| REQ-0319 | B | pytest: unit/integration/api/db/etl/e2e | 66 | ШАГ 12 | IMPLEMENTED — suite ~500; прогон 2026-10-03: 495 passed, 1 skipped (3 pre-existing td016 failures) |
| REQ-0320 | A | 13 обязательных финансовых тестов | 67 | ШАГ 12 | IMPLEMENTED — `tests/test_finance_integrity.py` |
| REQ-0321 | B | Dockerfile + docker-compose (app + postgres) | 68 | ШАГ 13 | IMPLEMENTED — `docker/Dockerfile` (non-root, healthcheck), `docker/docker-compose.yml` |
| REQ-0322 | C | CI/CD pipeline: lint→typecheck→unit→integration→build→migration check→deploy→health | 69 | ШАГ 14 | IMPLEMENTED (частично) — `.github/workflows/ci.yml`: schema→audit→alembic→pytest→ruff→mypy; deploy — ШАГ 15 |
| REQ-0323 | B | Health endpoint: application, database, critical dependencies | 70 | ШАГ 9 | IMPLEMENTED — `/health` с проверкой БД |
| REQ-0324 | B | Structured logging; не логировать пароли/токены/ключи | 71 | DAY 3 | IMPLEMENTED — `app/core/logging.py` (request_id, user_id); `tests/test_logging.py` |
| REQ-0325 | B | Monitoring: app/db/Kaspi errors, failed sync/jobs, auth failures, health | 72 | ШАГ 16 | TESTED — `/monitoring/summary` (5xx gauge, БД-таблицы, sync_log 24ч, failed_logins); `tests/test_monitoring.py` (3) |
| REQ-0326 | B | Backup + schedule + retention + restore + restore test | 73 | ШАГ 16 | IMPLEMENTED (backup.sh+restore.sh, тест восстановления tests/test_backup_restore.py; расписание — по команде) |
| REQ-0327 | B | Performance: indexes, pagination, pooling, bulk inserts, SQL aggregation | 74 | сквозное | IMPLEMENTED (индексы в DDL, /kaspi/payouts limit/offset, пул SQLAlchemy, батчи в ETL, SQL-агрегация P&L) |
| REQ-0328 | B | Environments: development/testing/staging/production через env vars | 11 | DAY 3 | IMPLEMENTED — `APP_ENV`, `app/core/config.py` env-only |
| REQ-0329 | B | .gitignore, .env.example; не коммитить секреты | 10 | DAY 1 | IMPLEMENTED |
| REQ-0330 | C | Git с самого начала, история шагов 001..017 | 9 | сквозное | IMPLEMENTED |
| REQ-0331 | B | TECHNICAL_DEBT.md | 125 | сквозное | IMPLEMENTED |
| REQ-0332 | B | ADR | 126 | DAY 1 | IMPLEMENTED |
| REQ-0333 | A | Rollback strategy для каждого релиза | 133 | ШАГ 15 | IMPLEMENTED — `docs/ROLLBACK_PLAN.md` (БД/деплой/конфигурация; verify-таблица) |
| REQ-0334 | A | Final project audit (10 аудитов) | 134 | ШАГ 16 | PENDING |

---

## DEFERRED (§1 — не удалено, отложено с обоснованием)

| ID | Требование | Причина | Шаг реализации | Зависимости |
|---|---|---|---|---|
| DEF-001 | Расчёт налогов (ИП, ОПВ/СО/ВОСМС) | Неизвестен налоговый режим (OQ-001); §75 запрещает зашивать ставки | После ответа OQ-001 | tax_rates_history (создана) |
| DEF-002 | Аллокация накладных расходов (аренда/коммунальные/реклама) на заказы | Правило распределения неизвестно (OQ-010); §37 требует только Direct Cost | ШАГ 11 или позже | Ответ OQ-010 |
| DEF-003 | Складской учёт остатков материалов (приход/расход/остаток, MRP-планирование) | §48–49 требуют цену и расход на заказ; полный склад не затребован (OQ-015) | Отдельный milestone | Ответ OQ-015 |
| DEF-004 | Мультивалютность | §15 фиксирует KZT (OQ-014) | Не планируется | Ответ OQ-014 |
| DEF-005 | Реальные endpoint'ы Kaspi API | §42/§94 запрещают выдумывать API | ШАГ 3 после OQ-003 | Официальная документация |
| DEF-006 | PDF-экспорт отчётов | §62 — «при необходимости» | ШАГ 11 | Excel-экспорт |
| DEF-007 | Интеграция банковских выписок | §51 не требует интеграции, только счета (OQ-009) | Отдельный milestone | Ответ OQ-009 |
| DEF-008 | Ротация ключа шифрования (скрипт перешифрования) | §19 требует документировать стратегию — сделано в ADR-009; реализация после появления реальных токенов | DAY 3 / ШАГ 3 | key_version (создана) |
