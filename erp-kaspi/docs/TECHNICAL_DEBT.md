# TECHNICAL DEBT (§125)

Осознанные упрощения, оставленные временно. Каждое имеет ID, причину, риск,
приоритет и план устранения.

| ID | Описание | Причина | Риск | Приоритет | План устранения |
|---|---|---|---|---|---|
| TD-001 | `operations` — единая таблица без партиционирования | На ШАГЕ 1 объём данных неизвестен; преждевременное партиционирование усложнило бы миграции | LOW (деградация запросов при >10 млн строк) | LOW | Партиционирование по `operation_date` (RANGE) при приближении к 5 млн строк; индексы уже совместимы |
| TD-002 | `fn_audit()` маскирует секреты по фиксированному списку имён колонок | Простое и предсказуемое решение; generic-подход через метаданные усложнил бы триггер | MEDIUM (новая колонка с секретом не попадёт в список) | MEDIUM | На ШАГЕ 8 добавить проверку в CI: любая новая колонка с именем из паттерна `%token%/%secret%/%password%/%key%` должна быть в списке маскирования |
| TD-003 | ~~Escape-hatch `app.allow_financial_delete` в `fn_forbid_delete()`~~ **RESOLVED (2026-09-16)**: hatch дополнительно закрыт членством в роли `erp_migrator` (fail-closed); `erp_app` не может ни обойти его, ни получить членство сам (TD-008) | Аварийное обслуживание выполняется под `erp_migrator` (владелец схемы); `SET LOCAL` в транзакции остаётся, но без роли не действует | ~~MEDIUM/HIGH~~ — закрыт: обход был воспроизведён и закрыт (см. обновление 2026-09-16) | — | Регрессионный тест `test_delete_escape_hatch_requires_migrator_role` фиксирует закрытие |
| TD-004 | `audit_log` растёт без ограничения и без партиционирования | На ШАГЕ 1 неизвестна интенсивность записи | MEDIUM (рост объёма БД) | MEDIUM | ШАГ 16: партиционирование по `created_at` (месяц) + политика retention с выгрузкой в архив (не удаление — §16) |
| TD-005 | Проверка знака и категории операции реализована триггером, а не CHECK-констрейнтом | CHECK не может обращаться к другой таблице (`operation_types`) | LOW | LOW | Приемлемо как окончательное решение; альтернатива — денормализация `direction`/`kind` в `operations`, что создаёт риск расхождения |
| TD-006 | `order_items.product_id` допускает NULL с денормализованным `product_name` | Заказы из Kaspi могут содержать позиции, отсутствующие в каталоге | LOW | LOW | ШАГ 6: авто-создание позиции каталога при импорте либо отчёт «неопознанные товары» |
| TD-007 | `v_ip_financial_summary` агрегирует на лету, без материализации | Объём данных на старте мал; MATERIALIZED VIEW требует стратегии обновления | LOW (медленный dashboard при больших объёмах) | LOW | ШАГ 11: при p95 > 1 s — MATERIALIZED VIEW + REFRESH по расписанию либо инкрементальные агрегаты |
| TD-008 | ~~Роль `erp_app` владеет схемой~~ **RESOLVED (2026-09-15)**: `scripts/split_db_roles.sh` — `erp_migrator` владеет схемой и единственный прогоняет миграции; `erp_app` — DML-only (`REVOKE CREATE ON SCHEMA public`); доказано на чистой БД (см. ниже) | — | — | — | Применён к erp_test, erp_dev, erp_mig_test; CI и docker-провижининг приведены в соответствие |
| TD-009 | Нет ограничения на длину `comment`/`reason`/`delivery_address` (TEXT) | Соответствует исходному ТЗ (§22, §23, §27, §28) | LOW (потенциальный abuse больших значений) | LOW | ШАГ 9: валидация длины в Pydantic-схемах на входе API |
| TD-010 | `payroll_summary.ip_id` допускает NULL | Сотрудник может работать на нескольких ИП; правило распределения ФОТ между ИП неизвестно (OQ-010) | MEDIUM (отчёт «зарплата по ИП» будет неполным) | MEDIUM | После ответа OQ-010: либо NOT NULL, либо отдельная таблица распределения |
| TD-011 | ~~Пароль в `repr`~~ РЕШЕНО: `DatabaseSettings.password` обёрнут в `Secret` | — | — | — | `Secret` маскирует repr()/str()/logging; тесты `test_core_database.py` (TD-011 block) фиксируют поведение |
| TD-012 | `scripts/audit_step1.py` не идемпотентен: повторный прогон на той же БД падает на `ip_entities_bin_key` | Скрипт создаёт фикстуры для поведенческих тестов и не откатывает их | LOW (мешает повторной проверке, ложное впечатление регрессии) | LOW | ШАГ 13: обернуть поведенческие проверки в транзакцию с ROLLBACK либо использовать `ON CONFLICT DO NOTHING`; обход — прогон на пересозданной БД |
| TD-018 | Cross-IP transfer POSTED correction/reversal не реализован; одиночный reversal запрещён ADR-015 | Нет подтверждённой бизнес-семантики для компенсирующего парного ledger-события; не выводить её из generic related_operation_id | MEDIUM — пока нет поддерживаемого correction workflow для ошибочного POSTED transfer | HIGH перед production rollout исправлений Transfer | Следующий FINANCE-OPS-001 блок: сопоставить ADR-007/OQ-006/требования, согласовать semantics, затем спроектировать atomic paired reversal и проверить idempotency, audit, auth и rollback до реализации |
| TD-019 | PARTIALLY RESOLVED (2026-10-01): шаг 2 выполнен — `trg_raw_kaspi_payloads_forbid_delete` + `trg_import_batches_forbid_delete` (тот же `fn_forbid_delete()` и migrator-hatch; schema.sql §7.2 = SoT, миграция `0008_provenance_delete_guard`); порядок purge в `conftest.py` исправлен (children → provenance parents → sync_log). Остаток: шаг 1 (ADR-016, BLOCKED — reversal semantics) и шаг 3 (включить поля в FIN-OPS-0001 guard) | Шаг 2: `raw_kaspi_payloads`/`import_batches` защищены от DELETE; остаток — `reverse_operation()` перезаписывает comment до ADR-016 | MEDIUM | MEDIUM | 1) ADR-016 reversal с отдельным `reason` (BLOCKED); 3) затем включить поля в FIN-OPS-0001 guard |

## Обновления (2026-09-07)

- TD-011 — **RESOLVED**: `Secret`-обёртка в `app/core/config.py` (repr/str/format маскируют); тесты `test_core_database.py` (§2a) фиксируют контракт.
- TD-012 — **RESOLVED**: `scripts/audit_step1.py` переведён на транзакцию с ROLLBACK; повторный прогон на той же БД корректен.
- TD-013 — `requirements.txt` не закреплял `bcrypt`/`PyJWT`, хотя `app/core/security.py` импортирует их напрямую. Исправлено: `bcrypt==5.0.0`, `PyJWT==2.10.1` добавлены (безопасность сборки, §10).
- TD-014 — PnL-сервис фильтровал по `direction` ('INCOME'/'EXPENSE'), а в БД `direction` — только 'IN'/'OUT' (chk_operation_types_direction); корректный атрибут — `kind`. Исправлено: сервис использует `kind`, согласовано с `v_ip_financial_summary` (§63).
| TD-015 | Rate limit `/auth/login` — shared sliding window в Postgres (`auth_rate_events`, миграция 0004, ADR-014) | **RESOLVED (2026-09-19)** — см. раздел ниже | — | — | — |

## Обновления (2026-09-14) — внешний аудит, Phase 0

- TD-013 — фактическое расхождение устранено: `requirements.txt` теперь действительно содержит
  `bcrypt==5.0.0`, `PyJWT==2.13.0` (в версии, закрытой для CVE-2026-48526), `email-validator==2.3.0`.
  Создан `requirements-dev.txt` (pytest/ruff/mypy/pip-audit) — CI устанавливает оба файла.
- TD-014 — **Streamlit напрямую читал БД без аутентификации** — ЗАКРЫТО (Phase 2, 2026-09-14):
  login-gate `frontend/auth_gate.py` (bcrypt-verify, lockout 5/15 мин из `app.core.security`,
  audit_log LOGIN/LOGIN_FAILED/LOGOUT в standalone-транзакции, §52–§55). Данные не рендерятся
  до успешного `authenticate()`; multi-IP изоляция — через `ip_guard()`/`restrict_ip_choices()`
  (тот же `app.core.policy`, что и в API). Читаем SQL напрямую и без обхода через FastAPI —
  осталось TD-016 (read-only, покрыто guard'ами на уровне SQL WHERE).
  Phase 2 (2026-09-14): login-gate добавлен (`frontend/auth_gate.py` — тот же bcrypt/lockout/
  audit_log, что и в API); UI-сессия аутентифицируется перед первым рендером данных.
  Остаточный риск сужен до read-SQL-канала (см. TD-016).
- TD-015 — backup.sh/restore.sh не дают PITR/offsite/restore-drill; erp_app в prod не должен иметь DDL
  (см. TD-008). Phase 1.
- `alembic check` добавлен в CI; drift моделей против миграций устранён (комментарии IpEntity,
  materials.current_price, operation_types, operations.amount, orders.total_amount/kaspi_commission,
  kaspi_payouts.net_amount).

## Обновления (2026-09-14) — закрытие аудита

| ID | Статус | Комментарий |
|---|---|---|
| TD-013 | RESOLVED (заново) | `bcrypt`, `PyJWT==2.13.0` (CVE-2026-48526), `email-validator` — в `requirements.txt`; `pytest`, `ruff`, `mypy`, `pip-audit` вынесены в `requirements-dev.txt`; CI обновлён |
| TD-014 | RESOLVED | Config drift шифрования: `crypto.py` теперь читает `ENCRYPTION_KEY` (`.env.example`, ADR-009); `KASPI_MASTER_KEY` — легаси-fallback |
| TD-015 | RESOLVED | Order state machine: Pydantic `OrderStatus` приведён к `chk_orders_status` (7 статусов); источник истины — `app.models.orders.ORDER_STATUSES` |
| TD-016 | INVENTORY (2026-09-16) | Streamlit всё ещё читает БД напрямую (`engine().connect()`, read-only) вместо FastAPI-прокси. Полная инвентаризация (45 точек доступа A1–D5), маппинг на API, 17 недостающих endpoints, Requirements Matrix REQ-TD016-01…11 и порядок миграции P1–P5: `docs/TD016_MATRIX.md`. Имплементация не начата; открыт OQ-TD016-01 (auth-путь UI) |
| TD-017 | **RESOLVED (2026-09-17, частично)** | PITR: WAL-архив проверен локально (archive_command, archived 1/fail 0); restore drill: pg_dump→sha256→pg_restore 36 таблиц PASS; RPO 24 ч / RTO ~1 мин (см. docs/BACKUP_DR.md). Остаток: off-site хранилище — BLOCKED (нет внешнего credential), автоматическое расписание — по команде владельца |
| TD-018 | OPEN (2026-09-26) | Парный reversal/correction Transfer отложен до отдельного подтверждения финансовой семантики; см. ADR-015 и следующий блок FINANCE-OPS-001. Одиночный reversal остаётся запрещённым. |
| TD-008 | **RESOLVED (2026-09-15)** | `scripts/split_db_roles.sh`: erp_migrator — владелец схемы, единственный роль для `alembic upgrade`; erp_app — SELECT/INSERT/UPDATE/DELETE + EXECUTE, `CREATE ON SCHEMA public` отозван. Доказательство на чистой БД erp_mig_test: `alembic upgrade head` под erp_migrator → split → полный pytest 279/279 под erp_app → `CREATE TABLE`/`DROP TABLE` под erp_app отклонены. Применено к erp_test и erp_dev; CI (шаг TD-008 role split) и docker-compose (`POSTGRES_USER: erp_migrator`, initdb создаёт DML-only erp_app) приведены в соответствие |

## Обновления (2026-09-16) — TD-003 root cause

- TD-003 — **RESOLVED (2026-09-16)**: escape-hatch `app.allow_financial_delete` в
  `fn_forbid_delete()` был обходИм. Root cause: PostgreSQL не позволяет отозвать право
  `SET` на session-custom GUC (`app.*`), поэтому `erp_app` мог выполнить
  `SET LOCAL app.allow_financial_delete = 'on'` и физически удалить финансовую строку.
  Воспроизведено: `erp_app` в erp_test удалил POSTED-операцию из `operations` с
  выставленным GUC (DELETE 1). Исправление в `database/schema.sql`
  (`fn_forbid_delete`): GUC теперь ДОПОЛНИТЕЛЬНО проверяет членство `current_user`
  в роли `erp_migrator` (владелец схемы, TD-008); fail-closed — если роль не
  существует, hatch не работает и DELETE остаётся запрещён. Служебные purge-операции
  (conftest §94, `test_login_gate` audit_log) переведены на migrator-подключение
  (`TEST_DATABASE_MIGRATOR_PASSWORD` / `DB_MIGRATOR_PASSWORD`); без пароля purge
  пропускается с предупреждением, не имитируется. Regression-тест:
  `tests/test_finance_integrity.py::test_delete_escape_hatch_requires_migrator_role`.
- Сопутствующее (2026-09-16): `requirements.txt` дополнен `PyYAML==6.0.3` — ETL
  (`etl/import_excel.py`) импортирует `yaml`, но зависимость не была закреплена;
  `scripts/apply_schema.sh` создаёт `erp_app` с `CREATEDB` (нужен restore-drill,
  `scripts/restore.sh` / `tests/test_backup_restore.py`); `pytest.ini` —
  `ignore::DeprecationWarning:starlette.testclient` (anyio>=4.11 эмитит
  BlockingPortal-deprecation со stacklevel=3, pytest атрибутирует его
  starlette.testclient).
- Верификация (2026-09-16): свежая БД → `apply_schema.sh` (×2, идемпотентность) →
  self-audit **624/624** → `split_db_roles.sh` → негативный repro TD-003: `erp_app` +
  GUC → DELETE отклонён; `erp_migrator` + GUC → служебный DELETE работает →
  чистая БД `alembic upgrade head` + `alembic check` → **280/280 pytest** под
  `erp_app` (DML-only) → ruff 0 → mypy (app) 0.

## Обновления (2026-09-17) — §17 AUTHENTICATION закрыт

- **Закрыто (2026-09-17)**: разрыв с MASTER PROMPT §17 — отсутствовали password-change
  (+ invalidation), token revocation, reset flow. Реализовано:
  `users.password_changed_at` (schema.sql + миграция 0002); `current_principal()`
  отвергает JWT, отчеканенные в/до момента смены пароля; `POST /auth/password-change`
  (self-service, rate-limited, требует текущий пароль); `POST /auth/users/{id}/reset-password`
  (ADMIN, сбрасывает lockout). LOGOUT остаётся клиентским discard (stateless JWT);
  серверная ревокация достигается сменой пароля / деактивацией / role-change re-check.
- **Закрыто (2026-09-17)**: UI-сессия доверяла кэшу session_state — деактивация/удаление
  пользователя и отзыв роли не действовали до рестарта сессии. `current_auth()` теперь
  перечитывает users/user_roles на каждом rerun (удалённый пользователь — выход без
  audit-FK; деактивированный — LOGOUT-запись).
- **Закрыто (2026-09-17)**: `_auth_attempts` рос безгранично (ключи ip:username не
  высвобождались) — добавлена opportunistic-очистка просроченных бакетов.
- **OQ-006 (открыта)**: MFA architecture — MASTER PROMPT §17 требует «MFA architecture»;
  проектирование отложено (нет провайдера/спецификации в среде; TOTP возможен как
  следующая итерация). Не блокирует остальные пункты §17.
- **Наблюдение**: единичный сбой `test_payroll_flow` сразу после аномального рестарта
  PostgreSQL (stale pid); 4 последовательных полных прогона — зелёные. Не код-дефект.

## TD-016 — RESOLVED (2026-09-17, финализация)

- **P1–P5 выполнены**: 24 новых read-endpoint (E1–E24 + `/ips`), каждый с
  `Depends(require_min_role)` + `ensure_ip_access[_or_all]`; frontend полностью
  переведён на `frontend/api_client.py` (HTTP-only boundary): auth_gate
  (login/me/logout через API), app.py, reports.py, admin_modules.py. Прямого
  DB-доступа во frontend больше нет (ADR-014, REQ-TD016-04).
- §17: `users.token_version` (миграция 0003) — server-side ревокация (logout /
  revoke-sessions / password change); LOGIN/LOGIN_FAILED пишутся в audit_log.
- Регрессия: **366/366 pytest**, ruff 0, mypy(app) 0, alembic upgrade+check PASS
  на чистой БД (erp_migration_check); grep-скан frontend — 0 точек прямого
  DB-доступа (`engine="openpyxl"` — не DB).
- **Mutation-доказательство (2026-09-17)**: удаление `ensure_ip_access_or_all`
  в `GET /operations` → `test_td016_02_restricted_sees_only_own_ip` FAIL
  (`assert 2 == 1` — restricted видит операции чужого ИП) → production-код
  восстановлен → тест PASS (48/48 TD-016-тестов зелёные). Boundary реально
  работает.
- Остаются открытыми (вне TD-016): TD-015 (distributed rate limiting), OQ-006
  (MFA), Docker runtime (нет daemon — BLOCKED), OQ-003 (Kaspi — BLOCKED по
  спецификации).

## TD-015 — RESOLVED (2026-09-19): distributed rate limiting

- **Shared backend**: таблица `auth_rate_events(bucket_key, occurred_at)` в
  существующем Postgres (ADR-014 — без Redis; миграция `0004_auth_rate_events`).
  Логика в `app/core/rate_limit_store.py` (count/record/prune), интеграция в
  `app/core/security.py::_shared_rate_limit`; включается env `RATE_LIMIT_SHARED=1`.
- **Политика отказа**: fail-open-to-local — 3 retry (бюджет 0.5 с), затем
  per-worker окно 5/60с продолжает защищать endpoint; login не блокируется
  (уточнение ADR-014 от 2026-09-19).
- **Hardening (2026-09-19)**: контеншн advisory lock не может подвесить login —
  перед `lock_bucket` в сессии limiter выполняется
  `SET LOCAL lock_timeout='600'` / `statement_timeout='700'` (иначе дефолтный
  30-секундный statement_timeout config превышал бы fail-open бюджет).
- **Модель**: `AuthRateEvent` добавлена в `app/core/auth.py` (drift-тесты
  моделей/схемы зелёные); FK на users нет намеренно (unknown usernames).
- **Доказательства**:
  - `tests/test_rate_limit_distributed.py` 7/7: unit (count/record/prune,
    window-boundary), integration 429 на 6-й попытке через API,
    **multi-process** (2 реальных процесса Python, суммарный бюджет ≤5),
    fail-open (SQLAlchemyError → локальное окно работает),
    lock-contention (удержанный advisory lock → <5 с, запись в локальное окно);
  - `tests/test_i18n_login.py` 6/6 — login gate локализован (RU default/KZ,
    единый I18N, no-leak инварианты);
  - полный pytest **379/379**, ruff 0, mypy(app) 0,
    alembic upgrade head + check PASS (erp_migration_scratch);
  - mutation (2026-09-19, на финальном коде с hardening): снятие
    `seen >= _AUTH_MAX_ATTEMPTS` → `test_shared_only_path_rejects_sixth...` FAIL
    (DID NOT RAISE) + multi-process FAIL (`8 <= 5`) → restore → 7/7 PASS;
    снятие `lock_bucket` — timing-dependent (проходит) → подтверждено.
- **BLOCKED-остаток**: multi-**replica** кластер (несколько хостов) не доказан —
  нет инфраструктуры; механика shared-state идентична multi-worker (та же
  таблица через сетевой DSN).

## TD-020 — OPEN (2026-10-02): Payroll period PAID after single summary payment (P1, BLOCKED)

- **Finding**: `pay_payroll_summary()` (`app/services/__init__.py:473-475`) sets `period.status = "PAID"` after paying a SINGLE employee's summary. `calculate_payroll()` (`:256`) refuses `CLOSED`/`PAID` periods. Result: a period with N employees cannot complete payroll for employees 2..N after employee 1 is paid.
- **Evidence**: `tests/test_payroll_flow.py:191` codifies current behavior (period PAID after 1 of 2 employees paid).
- **BLOCKED**: Correct "period PAID" semantic requires owner/business decision (all summaries PAID? explicit close action? per-employee periods?). Do NOT invent.
- **Research (2026-10-03)**: production patterns researched across Odoo / SAP HCM / Oracle Fusion Payroll / Dynamics 365 / 1C:ЗУП — none of them marks a period PAID as a side effect of a single employee payment. Findings, options A/B/C, pros/cons, and owner-vs-system decision split documented in `docs/TD-020_PAYROLL_STATUS_DECISION.md`.
- **A-vs-B deep analysis (2026-10-03)**: full chain RULE → DB MODEL → STATUS TRANSITIONS → PARTIAL/FAILED/REPEAT PAYMENT → REPORTING → AUDIT for both variants in `docs/TD-020_AB_ANALYSIS_2026-10-03.md`. Evidence-based recommendation: **B** (4/5 systems have no period-paid state at all; Odoo's package-Paid is an explicit user action, never auto-derived). Both variants require a `FOR UPDATE` period-row lock in `pay_payroll_summary()` (A has a silent race otherwise); B needs migration 0011 (CHECK minus PAID + data-migration PAID→CLOSED/PROCESSING); no frontend/report consumer of `period.status='PAID'` exists.
- **Discovered**: 2026-10-02 payroll chain audit (subagent + main agent verification).

## TD-021 — OPEN (2026-10-02): CLOSED period status unreachable (P2, NEXT)

- **Finding**: `payroll_periods.status` CHECK allows `CLOSED`, but no code path ever sets it. Lifecycle is OPEN → PROCESSING → PAID. `CLOSED` (and `CANCELLED` on summaries) are dead values.
- **Evidence**: `grep '"CLOSED"'` finds only the guard in `calculate_payroll()`; schema.sql:892 defines the constraint.
- **NEXT**: Either implement a close workflow or remove `CLOSED` from the CHECK constraint (requires migration).
- **Discovered**: 2026-10-02 payroll chain audit.

## TD-022 — OPEN (2026-10-03): Management dashboard missing (P1)

- **Finding**: Master Prompt §31 and FINANCE-OPS-001 §12 require a management dashboard (TODAY blocks: Sales/Refunds/Revenue/Expenses/Payroll/Materials/Commissions/Transfers/Net cash movement + period filters + drill-down). `frontend/app.py` has 10 modules, no dashboard exists.
- **Evidence**: REQ-0314 falsely claimed IMPLEMENTED (fixed 2026-10-03 → NOT IMPLEMENTED).
- **Note**: §13 cash-flow (`cash_flow()` + `/reports/cash-flow`, 2026-10-03) provides the data foundation. Dashboard is UI/UX work requiring owner prioritization of layout.
- **Discovered**: 2026-10-03 requirements gap audit (GAP-1).

## TD-023 — OPEN (2026-10-03): Turnover metric undefined (P1)

- **Finding**: Master Prompt §31 lists "turnover" (оборот) as required KPI; §11 requires its definition. Zero implementations exist. Revenue exists, turnover does not.
- **NEXT**: Owner must define turnover formula (gross volume? incl. transfers?) per §11 "one term, one formula" rule. Do NOT invent.
- **Discovered**: 2026-10-03 requirements gap audit (GAP-2).

## TD-024 — OPEN (2026-10-03): Receivables/payables not tracked (P1)

- **Finding**: Master Prompt §11 requires Receivable/Payable as core concepts; §31 lists as dashboard KPIs. Zero tables/views/endpoints exist.
- **NEXT**: Requires data model design (owner decision on semantics).
- **Discovered**: 2026-10-03 requirements gap audit (GAP-3).

## TD-025 — OPEN (2026-10-03): Formal metric definitions missing (P2)

- **Finding**: Master Prompt §11 requires METRIC ID definitions (Formula, Included/Excluded statuses, Source tables, etc.) for each KPI. Formulas are embedded in views/code without formal definitions.
- **NEXT**: Documentation work; does not block functionality.
- **Discovered**: 2026-10-03 requirements gap audit (GAP-4).

## TD-026 — OPEN (2026-10-03): ETL single-transaction import (P0)

- **Finding**: `etl/import_excel.py:375` wraps all N rows in one transaction. For 100k rows: holds locks for minutes, WAL grows unbounded (disk-full risk), any late error rolls back everything.
- **Fix plan**: Chunked commits (1000 rows/TX). The `import_fingerprint` unique constraint already prevents cross-chunk doubles. Batch status becomes PARTIAL on mid-import failure. Requires careful refactor of `run_import()` transaction management — do NOT rush (data corruption risk worse than perf issue).
- **Discovered**: 2026-10-03 ops audit (P0-2).

## TD-027 — OPEN (2026-10-03): No graceful shutdown (P0)

- **Finding**: No lifespan handler; SIGTERM kills in-flight `POST /transfers` / `POST /payroll/calculate` mid-transaction. PG rolls back (safe), but client gets connection error with no way to know if the first attempt committed.
- **Fix plan**: Add FastAPI lifespan handler for graceful shutdown. Document Idempotency-Key usage for retry-after-error (C-1/C-2 already support this).
- **Discovered**: 2026-10-03 ops audit (P0-3).
