# Production ERP / Finance / Kaspi / MRP — Kazakhstan

Production-система управления бизнесом для нескольких ИП: Kaspi Магазин, Kaspi Pay,
финансовый учёт, заказы, мебельное производство, сдельная зарплата, себестоимость,
аналитика.

Реализуется по MASTER PROMPT v3.0 строго по шагам (§79). Автоматического перехода к
следующему шагу нет (§117).

## Статус

### Жизненный цикл тестовой БД (важно для §94)

1. `DB_NAME=erp_test DB_USER=erp_migrator DB_PASSWORD=… bash scripts/apply_schema.sh --recreate`
   затем `DB_NAME=erp_test DB_MIGRATOR_PASSWORD=… bash scripts/split_db_roles.sh` (TD-008)
2. `python scripts/audit_step1.py` → 652/652 (поведенческие проверки откатываются транзакцией)
3. `pytest -q` → полный PostgreSQL regression на изолированной тестовой БД.

Порядок в CI именно такой (audit → alembic → pytest). API/finance-тесты
коммитят фикстуры для реального ASGI-пути; очистка §94 выполняется в teardown
через `erp_migrator`. Полный suite требует `TEST_DATABASE_MIGRATOR_PASSWORD`
или `DB_MIGRATOR_PASSWORD`; запускай его только на disposable test DB.
ETL-тесты (`tests/test_etl.py`) выполняются в rollback-транзакции.

| | |
|---|---|
| Текущий шаг | **ШАГ 1 — DATABASE — ЗАВЕРШЁН**; DAY 2 (модели + Alembic) — ЗАВЕРШЁН |
| Milestone | M1 Architecture ✅, M2 Database ✅, M3 Backend Core — частично |
| Self-audit | **652 / 652** (2026-09-26); полный PostgreSQL pytest regression **416 / 416** — PASS |
| Миграции | `alembic upgrade head` с пустой БД → `0006_transfer_pair_integrity`; `current`, `heads`, `check` — PASS; `chk_transfers_posted_has_legs convalidated=true` |
| ORM | 32 таблицы SQLAlchemy 2.x; соответствие применённой схеме проверено 81 автотестом |
| Проверено на | PostgreSQL 15.19 (требование §13: 15+) |
| Следующий шаг | FINANCE-OPS-001 — Reversal semantics |

## Технологический стек (§7, §8)

Python 3.11+ · FastAPI · PostgreSQL 15+ · SQLAlchemy 2.x · Alembic · Pydantic v2 ·
httpx · Tenacity · Pandas · cryptography · pytest · Streamlit · Plotly · Docker

Node.js / React / Vue / Angular / TypeScript **не используются** (§8).

## Структура проекта (§12)

```
webapp/
├── app/                    # backend (ШАГ 5–9)
│   ├── api/  core/  models/  schemas/  services/
│   ├── integrations/  finance/  payroll/  production/  kaspi/
├── database/
│   └── schema.sql          # ✅ ШАГ 1 — полный DDL (2306 строк)
├── alembic/                # миграции (ШАГ 2)
├── etl/                    # Excel ETL (ШАГ 2)
├── frontend/               # Streamlit (ШАГ 10)
├── tests/                  # pytest (ШАГ 12)
├── scripts/
│   ├── apply_schema.sh     # ✅ применение схемы + self-audit
│   └── audit_step1.py      # ✅ self-audit ШАГА 1 (§90)
├── docker/                 # ШАГ 13
├── docs/
│   ├── PROJECT_PLAN.md         # ✅ PLAN v1.0, 14 дней, milestones, риски
│   ├── REQUIREMENTS_MATRIX.md  # ✅ трассируемость REQ-ID → файл → тест
│   ├── OPEN_QUESTIONS.md       # ✅ 15 открытых вопросов (§95)
│   ├── TECHNICAL_DEBT.md       # ✅ 18 записей техдолга (§125)
│   ├── PROGRESS.md             # ✅ метрики и дневные отчёты
│   └── ADR.md                  # ✅ ADR-001..015
├── .env.example            # ✅ шаблон окружения (§10, §11)
└── .gitignore              # ✅ запрет коммита секретов (§10)
```

## Что готово: база данных

**32 таблицы** = 17 исходных (§80, сохранены точно — §81) + 14 расширений (§82) + security extension `auth_rate_events` (TD-015/ADR-014).

Исходные 17: `ip_entities`, `kaspi_api_credentials`, `operation_types`, `categories`,
`counterparties`, `orders`, `raw_kaspi_payloads`, `operations`, `transfers`, `sync_log`,
`employees`, `work_operation_types`, `rates_history`, `work_entries`, `payroll_periods`,
`payroll_summary`, `payroll_payments`.

Расширения (ARCHITECTURAL EXTENSION): `products`, `order_items`, `materials`,
`order_materials`, `kaspi_payouts`, `kaspi_payout_items`, `accounts`, `users`, `roles`,
`user_roles`, `audit_log`, `order_status_history`, `tax_rates_history`, `import_batches`,
`auth_rate_events` (TD-015 security/operations extension).

| Элемент | Кол-во |
|---|---|
| Таблицы в `database/schema.sql` / Alembic head | 32 / 32 (32 app tables; `alembic_version` исключена) |
| Views | 5 (3 обязательных §63 + 2 помеченных extension) |
| Индексы в `database/schema.sql` / Alembic head | 120 / 121 (Alembic head includes PK `alembic_version`) |
| Триггеры | 63 |
| CHECK-констрейнты приложения (`public` schema) | 108 |
| FOREIGN KEY | 62 |
| EXCLUDE-констрейнты | 3 |
| Helper-функции | 15 |
| Seed-записей | 48 (7 типов операций + 12 категорий + 11 видов работ + 11 ставок + 7 ролей) |

### Гарантии целостности на уровне БД

- **§16 Запрет DELETE** финансовых данных — триггер `fn_forbid_delete()` на
  `operations`, `transfers`, `payroll_payments`, `payroll_summary`, `kaspi_payouts`,
  `kaspi_payout_items`, `work_entries`, `audit_log`.
- **§32 Rate freeze** — `work_entries.rate_at_time` заполняется триггером из
  `rates_history` и неизменяем. Изменение ставки не меняет старые записи (проверено).
- **§38 Material price freeze** — `order_materials.price_at_time` фиксируется, изменение
  `materials.current_price` не меняет историческую себестоимость (проверено).
- **§31 Нет пересечения периодов ставок** — `EXCLUDE USING gist` (btree_gist).
- **§27 Знак суммы** соответствует `operation_types.direction`; `amount != 0`.
- **§28/§76 Transfer между разными ИП** создаёт атомарные `TRANSFER_OUT`/`TRANSFER_IN`
  с равными противоположными суммами; активность и принадлежность обоих счетов проверяются
  сервером/композитными FK. Transfer связан с обеими legs, идемпотентен и исключён из P&L;
  validated `chk_transfers_posted_has_legs` + deferred triggers обеспечивают парность.
- **§41/§57 Идемпотентность** — partial UNIQUE-индексы: повторный `external_id`, повторный
  `import_fingerprint`, двойная выплата зарплаты и второй ACTIVE-токен отклоняются БД.
- **§54/§71 Audit log** — generic-триггер на 19 таблицах, секреты маскируются
  (`password_hash`, `encrypted_token` → `***MASKED***`, проверено).
- **§55 Пароли** — CHECK допускает только bcrypt/argon2/scrypt-хеш; plaintext отклоняется.
- **§50 История статусов заказа** — пишется автоматически.
- **§56 updated_at** — триггер на 25 таблицах.

### Обязательные views (§63)

`v_ip_financial_summary` · `v_payroll_due` · `v_work_entries_detail`

Extensions (помечены явно): `v_order_costing` (Direct Cost / Gross Profit / Margin — §37),
`v_kaspi_reconciliation` (Order ↔ Payout ↔ Operation — §44).

## Запуск

```bash
# PostgreSQL должен быть запущен
sudo -u postgres pg_ctlcluster 17 main start

# Применить схему с нуля + проверить идемпотентность + self-audit
DB_NAME=erp_dev DB_USER=erp_migrator DB_PASSWORD=… scripts/apply_schema.sh --recreate

# TD-008: разделить роли — erp_migrator (DDL/владелец) и erp_app (DML-only)
DB_NAME=erp_dev DB_MIGRATOR_PASSWORD=… bash scripts/split_db_roles.sh

# Только self-audit по существующей БД (под DML-only ролью приложения)
DATABASE_URL=postgresql://erp_app:pass@127.0.0.1:5432/erp_dev \
  .venv/bin/python scripts/audit_step1.py
```

### Роли БД (TD-008, наименьшие привилегии)

| Роль | Права | Кто использует |
|---|---|---|
| `erp_migrator` | Владелец схемы/таблиц; DDL; единственная роль для миграций | alembic, `apply_schema.sh`, деплой |
| `erp_app` | SELECT/INSERT/UPDATE/DELETE + USAGE sequences + EXECUTE functions; `CREATE` на схеме отозван | API, Streamlit, ETL, тесты |

`scripts/split_db_roles.sh` идемпотентен и завершается ошибкой, если erp_app всё ещё
владеет объектами. В docker-развёртывании роль создаётся `docker/initdb/03-create-app-role.sh`,
bootstrap-суперпользователь контейнера — `erp_migrator`.

## Данные и валюта

- Деньги — `NUMERIC(15,2)`, количество — `NUMERIC(15,3)`, время — `TIMESTAMPTZ` (§13).
- Хранение времени в **UTC**, отображение — **Asia/Almaty** (§14).
- Валюта — **KZT**, формат отображения `1 234 567,00 ₸` (§15).

## Что НЕ реализовано (осознанно)

`DEFERRED` (§1 — не удалено, отложено с обоснованием) и `BLOCKED` — полный перечень в
[`docs/REQUIREMENTS_MATRIX.md`](docs/REQUIREMENTS_MATRIX.md).

Ключевое:
- **Kaspi API endpoints** — `BLOCKED`. Официальная спецификация не предоставлена (OQ-003);
  §42/§94 запрещают выдумывать API.
- **Расчёт налогов** — `DEFERRED`. Налоговый режим неизвестен (OQ-001); §75 запрещает
  зашивать ставки. Создана `tax_rates_history`, значения не заполнены.
- **Seed бизнес-данных** (ИП, сотрудники, каталог, пользователь по умолчанию) — не создан
  сознательно (§94). Дефолтный пароль был бы дефектом безопасности (§55).

## Открытые вопросы

15 вопросов в [`docs/OPEN_QUESTIONS.md`](docs/OPEN_QUESTIONS.md). Блокирующие для
следующих шагов: **OQ-002** (структура Excel → ШАГ 2), **OQ-003** (Kaspi API → ШАГ 3–4).

## Деплой

**Внимание (ADR-006):** требуемый стек (PostgreSQL + FastAPI + Streamlit + Docker)
**не поддерживается** платформой разработки (Cloudflare Workers/Pages: нет Python-рантайма,
нет серверных процессов, нет PostgreSQL, нет Docker). Песочница используется как
dev/CI-среда. Production требует внешней инфраструктуры: VPS/Docker-хост либо managed
Postgres + PaaS. Стек не менялся — §8 требует Python-first, а SQLite/D1 ломает требования
§13/§14/§31/§57 к финансовой целостности.

- **Платформа (dev)**: PostgreSQL 15.19 в песочнице ✅
- **Production**: не развёрнут (ШАГ 15)
- **Обновлено**: 2026-09-26
