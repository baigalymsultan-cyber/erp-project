# PROGRESS (§110, §114)

## PROGRESS METRICS (§114)

| Метрика | Значение |
|---|---|
| Requirements (всего в матрице) | 98 REQ-ID + 8 DEFERRED |
| Requirements IMPLEMENTED/TESTED | 95 (61 TESTED + 34 IMPLEMENTED) |
| Requirements PENDING | 1 (REQ-0334) |
| Requirements BLOCKED | 2 (REQ-0304, REQ-0305 — OQ-003) |
| Requirements DEFERRED | 8 |
| Milestones завершено | 2 / 15 (M1, M2); M3 частично |
| Tasks (DAY 1) | 12 / 12 |
| Tasks (DAY 2) | 9 / 9 |
| Self-audit проверок | **652 / 652** (2026-09-26); полный PostgreSQL pytest **416 / 416** (2026-09-26); FINAL_AUDIT 10/10 PASSED (исторический прогон 2026-09-14) |
| Critical Bugs | 0 |
| Bugs найдено/исправлено (DAY 2) | 2 / 2 (BUG-002, BUG-003) |
| High Bugs | 0 |
| Technical Debt | 18 записей; HIGH закрыты: TD-008 (2026-09-15), **TD-003 (2026-09-16)**; TD-018 — reversal workflow OPEN |
| Security Issues | 0 открытых (TD-003 bypass закрыт 2026-09-16) |
| Open Questions | 15 (2 блокирующих) |

---

## SECURITY & CORRECTNESS AUDIT (2026-09-14)

Внешний аудит (OWASP API Top 10 / ASVS-ориентированный) нашёл 25 проблем; Phase 0 закрыта
в этой итерации. Факт: **259/259 pytest**, ruff 0 ошибок, mypy 0 ошибок, `alembic check` 0 drift.

| # | Проблема аудита | Статус | Где |
|---|---|---|---|
| 1 | requirements.txt не содержал bcrypt/PyJWT/email-validator | ИСПРАВЛЕНО | `requirements.txt`, `requirements-dev.txt` |
| 2 | CI вызывал ruff/mypy без их установки; нет pip-audit | ИСПРАВЛЕНО | `.github/workflows/ci.yml` |
| 3 | KASPI_MASTER_KEY vs ENCRYPTION_KEY (config drift) | ИСПРАВЛЕНО | `app/integrations/kaspi/crypto.py` (ENCRYPTION_KEY канонический, старое имя принято как fallback) |
| 4 | API OrderStatus ≠ БД state machine | ИСПРАВЛЕНО | `app/schemas/__init__.py` (7 статусов chk_orders_status) |
| 5 | POST /orders не сохранял OrderItems | ИСПРАВЛЕНО | `app/api/__init__.py` |
| 6 | total_amount ≠ items_total не отклонялся | ИСПРАВЛЕНО | 422 + тест |
| 7 | order_direct_cost считал материалы по цене продажи | ИСПРАВЛЕНО | `app/services/__init__.py` (order_materials.total_cost) |
| 8 | rate limiter: все клиенты в корзине "unknown" | ИСПРАВЛЕНО | реальный client IP (TRUST_PROXY) + username |
| 9 | locked_until не использовался | ИСПРАВЛЕНО | lockout 5 неудач → 15 мин (423 LOCKED) |
| 1,18,19,20 | Multi-IP авторизация (OWASP API1) | ИСПРАВЛЕНО | `app/core/policy.py` + enforce в API; UI-баги IN/OUT, DONE/DELIVERED, ips[0] |
| 21 | PayrollPeriodCreate без валидации дат | ИСПРАВЛЕНО (ранее) | model_validator |
| 22 | POST /kaspi/payouts принимал OrderCreate | ИСПРАВЛЕНО (ранее) | `KaspiPayoutCreate`; net_amount вычисляется |
| 24 | alembic check отсутствовал в CI | ИСПРАВЛЕНО | CI step; модели синхронизированы (комментарии) |

Осталось открытым (Phase 1/2, не скрыто):
- Streamlit читает БД напрямую (engine().connect()); в UI нет login-экрана — session_scope(user_id)
  прокидывается, но user_id пока None без входа. Полное решение — UI через FastAPI (TD).
- DB-роли erp_migrator/erp_app не разделены; backup без PITR/offsite.
- Kaspi-интеграция остаётся BLOCKED (OQ-003) — осознанно.
---

## DAY 1 REPORT

**Goal**: M1 Architecture + M2 Database.

### Completed

1. **Планирование (§98–§121)**
   - `docs/PROJECT_PLAN.md` — PLAN v1.0: критический путь, 15 milestones с acceptance
     criteria, план на 14 дней с задачами/зависимостями/рисками/тестами, 8 рисков с
     митигацией, буфер 25%.
   - `docs/REQUIREMENTS_MATRIX.md` — 79 REQ-ID с трассируемостью
     Requirement → Implementation → File → Test → Status; 8 DEFERRED с обоснованием.
   - `docs/adr/ADR.md` — ADR-001..010, включая обязательные ADR-004 (Supabase не
     обязателен) и ADR-005 (PostgreSQL не exposed to Internet).
   - `docs/OPEN_QUESTIONS.md` — 15 вопросов; ответы не выдуманы (§94).
   - `docs/TECHNICAL_DEBT.md` — 10 записей.

2. **База данных (§80–§92)**
   - `database/schema.sql` — 1998 строк полного DDL без сокращений (§89).
   - 31 таблица: 17 исходных сохранены точно (§81) + 14 расширений, каждое помечено
     `ARCHITECTURAL EXTENSION` с указанием реализуемого требования (§82).
   - 11 helper-функций, 59 триггеров, 116 индексов, 105 CHECK, 60 FK, 3 EXCLUDE,
     5 views, 4 generated-колонки, идемпотентный seed.

3. **Инфраструктура проверки**
   - `scripts/audit_step1.py` — 623 проверки: структура (таблицы, колонки, типы,
     NOT NULL, DEFAULT, PK/FK/UNIQUE/CHECK, индексы, generated, триггеры, views, seed)
     + поведенческие тесты (rate freeze, price freeze, запрет DELETE, EXCLUDE, знак
     операции, идемпотентность, audit-маскирование, updated_at, двойная выплата).
   - `scripts/apply_schema.sh` — recreate + двойное применение + audit.
   - `.env.example`, `.gitignore` (§10, §11).

### Tests

- DDL применён на PostgreSQL 17.11 **дважды подряд** без ошибок — идемпотентность
  подтверждена (§87).
- Self-audit: **623 / 623 PASSED, 0 FAILED**.
- Проверка §89: в DDL нет TODO / FIXME / placeholder / «...».

### Bugs

| ID | Описание | Статус |
|---|---|---|
| BUG-001 | `chk_roles_code` требовал минимум 3 символа, что отклоняло обязательную роль `HR` (§52) при выполнении seed | ИСПРАВЛЕН: паттерн изменён на `^[A-Z][A-Z_]{1,49}$`. Обнаружен реальным применением DDL, а не ревью |

### Security

- Токены Kaspi: только `BYTEA`, формат AES-256-GCM задокументирован (ADR-009), ключ вне БД.
- Пароли: CHECK допускает только bcrypt/argon2/scrypt; plaintext отклонён (проверено).
- Audit log маскирует `password_hash` и `encrypted_token` (проверено).
- Дефолтный пользователь **не создан** сознательно — хардкод пароля был бы дефектом (§55).
- Fernet нигде не называется AES-256 (§19).

### Git

- Ветка `main`, коммит `002-database`.

### Requirements completed

REQ-0001..0015, REQ-0101..0117, REQ-0201..0228, REQ-0329..0332 (62 требования).

### Requirements remaining

REQ-0301..0328, REQ-0333, REQ-0334 (шаги 2–16).

### Blockers

| Blocker | Причина | Влияние | Решение |
|---|---|---|---|
| OQ-003 Kaspi API spec | Официальная документация не предоставлена | ШАГ 3–4 нельзя реализовать корректно; §42/§94 запрещают выдумывать API | Требуется документация от пользователя либо доступ к личному кабинету Kaspi |
| OQ-002 Структура Excel | Формат файлов неизвестен | ШАГ 2 потребует конфигурируемого маппинга; без примера файла маппинг будет угадан | Требуется пример Excel-файла |

### Next day (DAY 2 — только по команде пользователя, §79)

SQLAlchemy 2.x модели 1:1 со схемой + Alembic baseline-миграция + автотест соответствия
моделей DDL.

---

## DAY 2 REPORT

**Goal**: M3 (частично) — SQLAlchemy 2.x модели 1:1 со схемой, Alembic baseline, автотесты.

### Completed

1. **Модели (8 модулей, 31 таблица 1:1 со схемой)**
   `app/models/base.py`, `reference.py`, `auth.py`, `hr.py`, `orders.py`, `kaspi.py`,
   `finance.py`, `__init__.py`. Отражены 4 GENERATED-колонки, 3 EXCLUDE, 105 CHECK,
   60 FK (в т.ч. 3 циклических через `use_alter=True`), все 116 индексов,
   partial UNIQUE для идемпотентности. `configure_mappers()` проходит.

2. **Конфигурация и engine**
   `app/core/config.py` (env-only, §10; `safe_dsn()` маскирует пароль),
   `app/core/database.py` (engine c UTC на каждом соединении §15,
   `set_current_user()` — контракт с триггером `fn_audit()` §54).

3. **Alembic**
   `alembic.ini` (URL не хранится, UTC), `alembic/env.py` (autogenerate отключён,
   ADR-011), ревизия `0001_baseline_schema` — исполняет `database/schema.sql`
   дословно; `downgrade()` отказывает без `ERP_ALLOW_BASELINE_DOWNGRADE=1` (§16).

4. **Тесты (158)**
   `tests/test_models_match_schema.py` (81) — соответствие моделей применённой БД;
   `tests/test_alembic_baseline.py` (32) — граф ревизий, загрузчик SQL, инвентарь
   downgrade, защита §16, guard-условия upgrade;
   `tests/test_core_database.py` (36) — валидация настроек, маскирование секрета,
   UTC/timeout на живом соединении, `app.current_user_id` end-to-end до `audit_log`.

### Tests

| Проверка | Результат |
|---|---|
| `alembic upgrade head` на пустой БД `erp_alembic_test` | OK, `alembic_version = 0001_baseline_schema` |
| Объекты после миграции | 31 таблица, 5 views, 11 функций, 59 триггеров, 116 индексов, 105 CHECK, 60 FK, 3 EXCLUDE, seed 7+7 |
| `audit_step1.py` на БД, созданной Alembic | **623 / 623 PASSED** — миграция даёт результат, идентичный `psql -f schema.sql` |
| `upgrade head` повторно | без изменений, ревизия не переприменяется |
| `upgrade head` поверх БД от `apply_schema.sh` (§87) | OK, данные сохранены (operations=2, employees=1), таблиц 31 |
| `downgrade base` без opt-in (§16) | ОТКАЗ, схема цела (32 таблицы) |
| `downgrade base` с `ERP_ALLOW_BASELINE_DOWNGRADE=1` | чисто: 0 таблиц ERP, 0 views, 0 функций; `btree_gist` сохранён намеренно |
| Полный цикл `up → down → up` + audit | **623 / 623 PASSED** |
| pytest на `erp_test` / `erp_dev` / `erp_alembic_test` | **158 / 158 PASSED (прогон 2026-09-03)** на каждой |
| pytest c `-W error::DeprecationWarning` | 158 / 158 (2026-09-03) |
| §89 grep (TODO/FIXME/placeholder/bare pass/`...`) | чисто |

### Bugs

| ID | Описание | Как найден | Статус |
|---|---|---|---|
| BUG-002 | `database/schema.sql:30` содержит psql-метакоманду `\set ON_ERROR_STOP on`. Её исполняет только клиент `psql`; через драйвер (psycopg/Alembic/любой ORM) PostgreSQL отвечает `syntax error at or near "\"`. Baseline-миграция падала полностью | Реальным запуском `alembic upgrade head`, не ревью | ИСПРАВЛЕН: директива **оставлена** в `schema.sql` (она обеспечивает abort-on-error для psql-пути; удаление ослабило бы его — §1), а драйверный путь удаляет client-only строки перед исполнением. Неизвестная метакоманда прерывает миграцию, а не игнорируется молча |
| BUG-003 | `tests/test_models_match_schema.py::test_table_set_matches_database` падал после первого `alembic upgrade head`: в БД появилась служебная таблица `alembic_version`, которой нет в моделях | Прогоном тестов на БД после миграции | ИСПРАВЛЕН в тесте: `alembic_version` внесена в `UNMAPPED_INFRASTRUCTURE_TABLES`. ORM-модель для неё не создаётся сознательно — таблицей владеет Alembic. Тесты проходят и с ней, и без неё |

Дополнительно устранено предупреждение Alembic: `version_path_separator` → `path_separator`
(иначе Alembic делит пути по пробелам/запятым/двоеточиям).

### Security

- Пароль БД: только из окружения; `safe_dsn()` маскирует его, покрыто 3 тестами.
- Зафиксировано как TD-011: `repr(DatabaseSettings)` пароль **содержит** — факт закреплён
  тестом, чтобы не быть обнаруженным позже в логе; для диагностики обязателен `safe_dsn()`.
- `set_current_user()` приводит значение к `int`, строка вида `"1 OR TRUE"` отклоняется.
- Транзакционная локальность `app.current_user_id` проверена на одном физическом
  соединении: значение не переживает транзакцию (иначе аудит приписал бы действие
  чужому пользователю).
- `alembic.ini` проверяется тестом на отсутствие DSN и пароля.

### Requirements completed

REQ-0318 → **TESTED**.

### Not proven (§93)

- Модели проверены на **соответствие структуре**; CRUD-операции через ORM (каскады,
  поведение relationship при удалении, работа `Computed`-колонок при flush) не выполнялись.
- Baseline проверена на PostgreSQL 17.11 в песочнице; на 15.x не проверялась.
- `session_scope()` / `get_engine()` / `dispose_engine()` не покрыты тестами —
  это process-wide синглтоны, их тест требует изоляции процесса (ШАГ 13).
- Alembic-миграция «вторая ревизия поверх baseline» не создавалась: сценарий эволюции
  схемы будет проверен, когда появится первая реальная миграция.

### Blockers

Без изменений: OQ-002 (пример Excel) блокирует корректный маппинг ШАГа 2;
OQ-003 (спецификация Kaspi API) блокирует ШАГи 3–4 (§94).

### Next day (DAY 3 — только по команде пользователя, §79)

`app/core/crypto.py` (AES-256-GCM для токенов Kaspi, ADR-009), `app/core/logging.py`
(структурные логи с маскированием), Pydantic v2-схемы справочников. ШАГ 2 (Excel ETL)
требует ответа на OQ-002.

---

## CONFLICTS (§5)

Обнаруженных противоречий между требованиями MASTER PROMPT: **0**.

Одно граничное решение, требующее фиксации:
- §52 требует роль `HR` (2 символа); мой первоначальный валидатор кода роли требовал 3+.
  Требование ТЗ приоритетнее технического предпочтения — исправлен валидатор, а не
  требование (§1: запрещено молча изменять требования).

---

## 2026-09-07 — сессия продолжения (Sultan/alembic)

- Проект перемещён/консолидирован в `Sultan/alembic` — рабочая копия с миграциями; suite прогоняется отсюда.
- §16: реализовано сторно операций — `POST /operations/{id}/reverse` (service `reverse_operation`), статус REVERSED, строка остаётся (append-only), повторное сторно запрещено; `pnl()` учитывает только POSTED; API-тест `test_operation_reversal`.
- §63/§54: исправлен PnL-сервис — фильтр по `kind` (INCOME/EXPENSE), а не по `direction` (IN/OUT); согласовано с `v_ip_financial_summary`.
- §54: `get_db` декодирует bearer и ставит `app.current_user_id` — аудиторские строки пишутся с действующим пользователем; `/health` и `/auth/login` остаются с NULL.
- `requirements.txt`: добавлены прямые зависимости `bcrypt`, `PyJWT` (ранее тянулись транзитивно).
- Проверки: pytest 235 passed/1 skipped (erp_test), ruff — чисто, mypy app — чисто.
- Статус миграций: baseline `0001_baseline_schema` (исполняет database/schema.sql вербатим); `alembic upgrade head` требует пароль в env (DATABASE_PASSWORD) — в CI покрыто шагом «Alembic baseline on a clean database».

---

## 2026-09-14 — UI/UX OVERHAUL (ШАГ 10, §58–62)

Аудит + редизайн Streamlit-дашборда (frontend/app.py, frontend/reports.py).

### Bugs найдено/исправлено

| ID | Описание | Root cause | Статус |
|---|---|---|---|
| BUG-004 | REQ-0008 нарушался во всём UI: деньги как `1,234,567.00` вместо `1 234 567,00 ₸` (§15) | Форматирование дублировалось Python-`f" {v:,.2f}"` в каждом модуле | ИСПРАВЛЕН: единая точка правды `KZT_SQL` (to_char+translate) и `fmt_money()` |
| BUG-005 | KPI «Чистая прибыль» на «Операциях» завышался (68,08 M вместо 54,54 M): расход суммировался без инверсии знака | §27: OUT-операции хранятся отрицательными; канонический `pnl()` инвертирует, мой фильтрованный KPI — нет | ИСПРАВЛЕН + сверено с `pnl()` и `v_ip_financial_summary` |
| BUG-006 | `module_settings` падал: `DatabaseSettings.environment` не существует | Несуществующий атрибут | ИСПРАВЛЕН: `os.environ.get("APP_ENV")` |
| BUG-007 | psycopg `SyntaxError at or near ":"` при фильтрах `:kind::text`/`:st`/`:act`/`:emp` | psycopg не поддерживает F-string-касты на параметрах | ИСПРАВЛЕН: `CAST(:x AS type)` (6 мест) |
| BUG-008 | KPI «Зарплата» падал: колонки `amount_calculated` нет во `v_payroll_due` | SQL написан по памяти, не по схеме | ИСПРАВЛЕН: `total_calculated` (сверено с information_schema) |
| BUG-009 | `pnl_dataframe` — мёртвый код; LIMIT 500/300 молча обрезал реестры | Legacy от раннего прототипа UI | УСТРАНЕН: серверная пагинация LIMIT/OFFSET + COUNT, caption «N строк · Стр. i/j» |

### UI/UX (§58–62)

- Единый дизайн-токен-слой (CSS-переменные, карточки KPI, professional enterprise-тема).
- Фильтры/поиск/сортировка/серверная пагинация: Операции (период, направление, ИП, поиск),
  Заказы (статус, поиск, donut по статусам), Производство (сотрудник, период), Товары (поиск, активные).
- Графики plotly из реальных данных: доход/расход по дням, статусы заказов, зарплатные KPI.
- Формат §15 везде: `1 234 567,00 ₸` (KPI, таблицы, графики, экспорт).
- Пустые состояния с понятным действием (Kaspi BLOCKED OQ-003 показывается честно, без моков).
- RU/KK i18n расширен; подтверждение/успех/ошибки формы бригадира сохранены (§61).

### Verification

- pytest: **252 / 252 PASSED** (erp_test, PostgreSQL 17), self-audit DDL 624/624, ruff — чисто.
- Скриншоты: `docs/screenshots/ui_*.png` (реальные данные erp_dev, без моков).

---

## 2026-09-14 — UI/UX OVERHAUL (§58–62, аудит 30 областей)

### Audit findings (evidence-based)

| ID | Описание | Статус |
|---|---|---|
| BUG-004 | §15/REQ-0008 нарушался во всём UI: деньги `1,234,567.00` вместо `1 234 567,00 ₸` | ИСПРАВЛЕН: единый `fmt_money()` + `KZT_SQL` (to_char+translate) во всех таблицах/KPI/отчётах |
| BUG-005 | KPI «Чистая прибыль» на «Операциях» считал `SUM(amount) WHERE EXPENSE` без отрицания — расход хранится отрицательным (§27), прибыль завышалась (61,312,000+6,771,000 вместо 61,312,000−6,771,000) | ИСПРАВЛЕН: `-o.amount`, согласовано с `pnl()`/v_ip_financial_summary |
| BUG-006 | `module_settings` обращался к несуществующему `settings.environment` (AttributeError, падение модуля) | ИСПРАВЛЕН: `os.environ.get("APP_ENV")` |
| BUG-007 | psycopg-касты `:param::type` в 7 SQL — `SyntaxError at or near ":"` (падали «Товары», «Зарплата», «Операции» с фильтрами) | ИСПРАВЛЕН: `CAST(:p AS type)` |
| BUG-008 | KPI зарплаты запрашивал несуществующую колонку `amount_calculated` (вьюха даёт `total_calculated`) — падение модуля | ИСПРАВЛЕН |
| BUG-009 | `pnl_dataframe()` — мёртвый код; caption пагинации дублировал total («427 из 427»); `None` в ячейках таблиц; LIMIT 500 молча обрезал данные | ИСПРАВЛЕНЫ: график подключён, «427 строк · Стр. 1/18», «—», серверная пагинация LIMIT/OFFSET+COUNT |

### UI/UX (§58–§62, стек не изменён — Streamlit+Plotly, без React/Node)

- Дизайн-система: CSS-токены (палитра, карточки, KPI-тонации good/bad), единая типографика.
- Все 10 модулей (§60): фильтры (период/направление/ИП/статус/сотрудник), поиск, серверная пагинация, empty states, KPI-карточки сверху.
- Операции: график доход/расход по дням (Plotly). Заказы: donut по статусам. Зарплата: KPI начислено/выплачено/остаток. Банки: счета + Kaspi payouts + sync_log. Отчёты: Excel-экспорт сохранён.
- Языки RU/KK (§58) расширены. Данные — только реальные (вьюхи §63/таблицы); отсутствующая функциональность показана честно (Kaspi API — BLOCKED, OQ-003).

### Verification

- pytest: **252/252 PASSED** (включая 10 AppTest-модульных, erp_test); self-audit DDL: **624/624** (erp_test recreate).
- ruff: 0 ошибок. Скриншоты: docs/screenshots/ui_*.png (erp_dev, 427 операций).
- root ↔ Sultan/alembic: **467/467 IDENTICAL** (хеш-сравнение git-tracked файлов проекта).

---

## ОБНОВЛЕНИЕ (2026-09-14) — исправление аудита (Phase 0)

Фактический запуск в чистой среде выявил расхождение документации с кодом. Исправлено:

1. **requirements.txt** — добавлены `bcrypt`, `PyJWT==2.13.0`, `email-validator`; создан
   `requirements-dev.txt` (pytest/ruff/mypy/pip-audit); CI устанавливает оба файла (аудит #1, #4, #5).
2. **CI** — добавлены шаги `alembic check` (модели ↔ миграции, аудит #24) и `pip-audit`
   (зависимости, аудит #11); CI-файл перенесён в проект.
3. **Шифрование** — `crypto.py` читает `ENCRYPTION_KEY` (легаси `KASPI_MASTER_KEY` — fallback);
   config drift устранён (аудит #3).
4. **OrderStatus** — Pydantic enum приведён к схеме БД: NEW/APPROVED/ASSEMBLING/DELIVERING/
   COMPLETED/CANCELLED/RETURNED (аудит #6).
5. **POST /orders** — OrderItem теперь сохраняются; при `total_amount != items_total` — 422
   вместо молчаливого `"consistent": false` (аудит #6).
6. **order_direct_cost()** — материалы считаются по `order_materials.total_cost`
   (замороженная закупочная цена), а не по отпускной `order_items.unit_price` (аудит #7).
7. **Login** — rate limit по реальному IP (+`TRUST_PROXY`) и username, а не общий бакет
   `"unknown"` (аудит #8); включён account lockout через `users.locked_until`
   (5 неудач → 15 минут, сброс при успехе, аудит #9).
8. **Multi-IP авторизация (OWASP API1)** — новый `app/core/policy.py`:
   ADMIN/DIRECTOR → все ИП, остальные — только ИП привязанного сотрудника;
   проверки внедрены в POST /operations, /transfers, /orders, GET/POST /kaspi/payouts,
   GET /operations/{id}, /payroll/summary (аудит #1).
9. **UI-баги** — фильтр направлений приведён к `ot.kind` (INCOME/EXPENSE, аудит #18);
   статусы заказов в KPI приведены к `chk_orders_status` (аудит #19); Foreman больше
   не подставляет «первый попавшийся» ИП — явная ошибка без привязки (аудит #20);
   Streamlit пишет `user_id` в аудит через `session_scope(user_id=...)`.
10. **Alembic** — устранён comment-drift моделей vs схемы; `alembic check` зелёный.
11. **Тесты** — новый `tests/test_audit_fixes.py`: изоляция ИП (200/403), сохранность
    OrderItem, 422 на расхождение сумм, lockout, rate limit, payout-схема.

**Верификация**: pytest 259/259 PASS (было 252; +7 новых), ruff 0 ошибок,
mypy 0 ошибок, `alembic check` PASS, Streamlit UI запускается
(скриншот: `docs/screenshots/dashboard_20260914.png`).

**Осталось открытым (не скрываем)**: Streamlit всё ещё имеет прямой read-only доступ к БД
(TD-016); нет PITR/offsite бэкапов (TD-017); DB-роли не разделены (TD-008);
Kaspi API остаётся BLOCKED (OQ-003).

---

## 2026-09-14 — PHASE 2: UI/UX + FRONTEND + PRODUCTION HARDENING (завершена)

### Что сделано

1. **Login-gate UI (TD-014, §52–§55)** — `frontend/auth_gate.py`: аутентификация по тем же
   семантикам, что и API (bcrypt, lockout 5→15 мин, счётчик неудач, is_active), аудит
   LOGIN/LOGIN_FAILED/LOGOUT с user_id (§54). Дефект, найденный тестами: аудиты
   LOGIN_FAILED/locked/inactive писались в откатываемую транзакцию и терялись —
   исправлено (`_audit_standalone`, собственный `session_scope`).
2. **IP-guard во всех модулях UI (OWASP API1)** — все SQL-запросы «Банков», «Заказов»,
   «Зарплаты», «Производства», «Отчётов» и формы бригадира фильтруются фрагментом
   `ip_guard()` (тот же `allowed_ip_ids`, что в API). Дополнительно закрыта утечка:
   дропдаун «Сотрудник» в Производстве показывал имена сотрудников всех ИП.
3. **frontend/reports.py** — все отчёты на вьюхах §63 (v_ip_financial_summary,
   v_kaspi_reconciliation, v_payroll_due, v_order_costing, operations, v_work_entries_detail)
   получили ip_guard; «Материалы» без ip_id (общий каталог) — без фильтра, задокументировано.
4. **Админ-меню по ролям (§53)** — секция «Администрирование» собирается из
   `ADMIN_MODULE_ROLES` через `has_min_role` (та же иерархия, что в API);
   VIEWER/BRIGADIER видят только разрешённые модули.
5. **i18n RU/KK (§58)** — ключи админ-секции и ролей присутствуют в обоих языках.

### Тесты (+18, итого 279)

- `tests/test_login_gate.py` (18): успешный вход + аудит, неверный пароль (счётчик +
  LOGIN_FAILED), lockout после порога, неактивный аккаунт, иерархия ролей, ip_guard
  (нет сессии → FALSE; ограниченный пользователь → только свой ИП; без привязки → deny all),
  restrict_ip_choices (без маркера «Все», пустой список), изоляция SQL-отчётов.
- `tests/test_streamlit_ui.py`: переписан под login-gate (AppTest аутентифицируется
  через session_state), smoke всех 10 модулей, админ-меню по ролям (ADMIN — 4 модуля,
  VIEWER — скрыто).

### Проверки

- pytest: **279/279 PASSED** (erp_test); ruff: 0 ошибок; mypy (app): 0 ошибок;
  `alembic check`: PASS (erp_dev).
- Скриншоты: `login_gate_20260914.png`, `dashboard_auth_20260914.png`,
  `reports_admin_20260914.png`, `reports_restricted_20260914.png` (erp_dev, 427 операций;
  бухгалтер без привязки к ИП видит пустые отчёты — изоляция подтверждена вживую).

### Остаточный технический долг (не скрываем)

- TD-016: Streamlit по-прежнему читает БД напрямую read-SQL вместо FastAPI-прокси
  (аутентификация теперь есть, сетевой boundary — ШАГ 15+).
- TD-017: нет PITR/offsite-бэкапов. TD-008: DB-роли не разделены. OQ-003: Kaspi API BLOCKED.


---

## PHASE 2 (2026-09-14) — UI/UX hardening: login-gate, RBAC-меню, IP-изоляция отчётов

### Сделано

1. **Login-gate** (`frontend/auth_gate.py`, TD-014/§52–§55): вход до любого рендера данных;
   bcrypt-verify и lockout (5 неудач → 15 мин) — те же константы `app.core.security`, что и в API;
   audit_log LOGIN/LOGIN_FAILED/LOGOUT; исправлен дефект, найденный тестами: аудит-строки
   неудачных попыток писались в откатывающуюся транзакцию и терялись → вынесены в
   standalone `session_scope` (`_audit_standalone`), §54 соблюдён.
2. **IP-guard в Отчётах** (`frontend/reports.py`): 6 запросов к ip-зависимым данным
   (v_ip_financial_summary, v_kaspi_reconciliation, v_payroll_due, v_order_costing,
   operations ×2, v_work_entries_detail) получили `WHERE {ip_guard(...)}`; «Материалы» —
   общий каталог без ip_id, изоляция не требуется.
3. **Производство**: дропдаун «Сотрудник» (`DISTINCT employee_name FROM v_work_entries_detail`)
   утекал имена сотрудников чужих ИП — закрыт `ip_guard`.
4. **Админ-меню по ролям** (`ADMIN_MODULE_ROLES` + `has_min_role`): Сотрудники→HR,
   Мониторинг/Пользователи→ADMIN, Аудит→DIRECTOR; та же иерархия §53, что в API;
   i18n-ключи (ru/kk) на месте.
5. **Тесты**: новый `tests/test_login_gate.py` (18): authenticate ok/invalid/lockout/inactive,
   аудит-строки, has_min_role, ip_guard SQL (FALSE/TRUE/IN-список), restrict_ip_choices,
   изоляция отчётного SQL на реальных данных erp_test; `tests/test_streamlit_ui.py` обновлён
   под login-gate (12): все 10 модулей рендерятся после аутентификации, экран входа блокирует
   sidebar, админ-меню скрывается у ACCOUNTANT без прав.

### Verification (всё зелёное)

- pytest: **279/279 PASSED** (было 259; +20), erp_test PostgreSQL 17.
- ruff (app/etl/frontend/tests/scripts): 0 ошибок; mypy (app): 0 ошибок; `alembic check`: PASS.
- Живые скриншоты erp_dev (реальные данные, без моков):
  `docs/screenshots/login_gate_20260914.png` (экран входа),
  `dashboard_auth_20260914.png` (админ, 10 модулей + чип роли),
  `reports_admin_20260914.png` (Отчёты: ИП «Мебель-Демо», §15-формат),
  `reports_restricted_20260914.png` (бухгалтер без привязки к ИП — пусто, ни одной чужой строки).
- root `alembic/` — единственный канонический (alembic.ini → script_location = alembic);
  `Sultan/` — пустой каталог-заготовка, дубликатов миграций нет.

### Осталось (честно)

- TD-016: Streamlit читает БД напрямую (read-only, все запросы покрыты ip_guard); полный
  boundary через FastAPI-прокси — ШАГ 15+.
- TD-017: нет PITR/offsite-бэкапов; TD-008: роли БД не разделены; OQ-003: Kaspi API BLOCKED.

---

## PHASE 3 (2026-09-15) — TD-008 закрыт: разделение ролей erp_migrator / erp_app

### Сделано

1. **`scripts/split_db_roles.sh`** (новый, идемпотентный): перенос владения схемой/
   таблицами/последовательностями/вьюхами/функциями на `erp_migrator`; erp_app —
   только SELECT/INSERT/UPDATE/DELETE + USAGE sequences + EXECUTE functions;
   `REVOKE CREATE ON SCHEMA public`; default privileges для будущих объектов.
   Проверка-доказательство в конце: 0 объектов, принадлежащих erp_app.
2. **Чистая БД (erp_mig_test)**: `alembic upgrade head` под erp_migrator (владелец БД —
   иначе trusted-расширение btree_gist не создаётся) → split → полный pytest 279/279
   под erp_app → негативные проверки: `CREATE TABLE`/`DROP TABLE` под erp_app отклонены
   (`permission denied`). `alembic check` под erp_app: PASS.
3. **Применено к erp_test и erp_dev**: erp_app читает/пишет (427 операций в erp_dev),
   DDL отклонён; `scripts/audit_step1.py` под erp_app на erp_test — 624/624 PASSED.
4. **CI** (`.github/workflows/ci.yml`): БД создаются с OWNER erp_migrator; schema
   применяется под erp_migrator; новый шаг «TD-008 role split» (через
   DB_ADMIN_HOST/DB_ADMIN_PASSWORD — без sudo) сразу после apply_schema; alembic
   baseline — erp_migrator; alembic check + pytest — erp_app (доказывает DML-only);
   pip-audit теперь также проверяет requirements-dev.txt.
5. **Docker** (`docker/docker-compose.yml`, `docker/initdb/03-create-app-role.sh`,
   `docker/Dockerfile`): bootstrap-суперпользователь контейнера — erp_migrator;
   initdb-скрипт создаёт erp_app DML-only после schema.sql; CMD запускает
   `alembic upgrade head` под erp_migrator, uvicorn — под erp_app.
6. **§94 fix**: conftest теперь очищает `users`/`user_roles` в конце сессии (раньше
   тестовые пользователи накапливались в erp_test — 140 записей — и ломали
   §94-проверку аудита). FK-безопасно: user_roles CASCADE, остальные SET NULL.

### Verification (всё фактическое)

- pytest: **279/279 PASSED** (erp_test, под erp_app DML-only); ruff: 0; mypy (app): 0;
- `alembic check`: PASS (erp_app на erp_test, erp_dev; erp_migrator на erp_mig_test);
- `audit_step1.py`: **624/624** на erp_test после прогона suite (§94 включая users);
- на erp_dev — 621/624 (3 §94-FAIL ожидаемы: демо-данные в dev-БД);
- негативные права: CREATE TABLE/DROP TABLE под erp_app → permission denied (erp_test,
  erp_dev, erp_mig_test).
- pip-audit 2.10.1 (локально, env-режим): 22 уязвимости — все в пакетах песочницы
  (pip, uv, wheel, lxml через сторонний linkedin-api), НИ ОДНОЙ в requirements.txt /
  requirements-dev.txt (сравнение поимённо). Полный `-r`-прогон делает CI (локально
  ensurepip в gVisor-песочнице падает — ограничение окружения, не проекта).

### Остаточное (честно)

- Docker build/startup не проверены — daemon в песочнице недоступен (BLOCKED);
  compose-файл и initdb-скрипт выверены вручную.
- TD-016 (Streamlit read-SQL вместо FastAPI-прокси), TD-017 (PITR/offsite),
  TD-003 (erp_app может SET app.allow_financial_delete — частично смягчено аудитом),
  OQ-003 (Kaspi API) — открыты.

---

## TD-003 CLOSED (2026-09-16) — актуальная verified state

### Что сделано

- **TD-003 (P0)**: escape-hatch `app.allow_financial_delete` в `fn_forbid_delete()`
  был обходим: session-custom GUC может выставить любая роль, поэтому `erp_app`
  мог `SET LOCAL app.allow_financial_delete='on'` и физически удалить финансовую
  строку. Воспроизведено фактически, затем закрыт root cause: hatch теперь
  дополнительно требует членства в роли `erp_migrator` (fail-closed).
  Детали и evidence — в `docs/TECHNICAL_DEBT.md`, обновление 2026-09-16.
- Попутно (среда, не архитектура): `requirements.txt` дополнен `PyYAML==6.0.3`
  (`etl/import_excel.py` импортирует yaml, но пакет не был закреплён — среда без
  общесистемного PyYAML падала на collection тестов ETL); `apply_schema.sh`
  создаёт `erp_app` с `CREATEDB` (restore drill создаёт scratch-БД под app-роль —
  так задумано `tests/test_backup_restore.py`); `pytest.ini` игнорирует
  DeprecationWarning из `starlette.testclient` (anyio>=4.11 эмитит его со
  stacklevel=3) и `anyio._lazyimport`.

### Verification (всё фактическое, 2026-09-16)

- Свежая БД: `DB_NAME=erp_test scripts/apply_schema.sh --recreate` → self-audit
  **624/624**; idempotency (второй apply) → PASS; то же для erp_dev;
- `scripts/split_db_roles.sh` (erp_test, erp_dev) → OK: erp_app DML-only, 0 owned objects;
- pytest: **280/280 PASSED** (erp_test, под erp_app; +1 — TD-003 regression
  `test_delete_escape_hatch_requires_migrator_role`; было 279);
- ruff: 0 ошибок; mypy (app): 0 ошибок; `alembic upgrade head` + `alembic check`: PASS
  (свежая БД erp_alembic-путь, миграция 0001 применяет актуальный schema.sql);
- негативный тест: `erp_app` + GUC → `DELETE` отклонён (restrict_violation);
  `erp_migrator` + GUC → служебный DELETE работает (maintenance path сохранён).

### Остаточное (честно, после 2026-09-16)

- Docker build/startup не проверены — daemon в песочнице недоступен (BLOCKED);
- TD-016 — инвентаризация завершена (2026-09-16): 45 точек прямого DB-доступа в frontend/,
  9 существующих API-endpoints покрывают малую часть, 17 недостающих endpoints, матрица
  REQ-TD016-01…11, порядок миграции P1–P5 → `docs/TD016_MATRIX.md`; имплементация не начата;
- Skills восстановлены (2026-09-16): 26/26 из workspace-архивов → `/home/workspace/Skills/`,
  аудит и маппинг на ERP → `docs/SKILLS.md`;
- v7 audit (2026-09-17): §17 AUTHENTICATION закрыт (password lifecycle + revocation +
  reset), UI-сессия ревалидируется из БД на каждом rerun, rate-limiter ограничен по
  памяти. Evidence: 288/288 pytest, ruff 0 (вкл. alembic/), mypy 0,
  self-audit 624/624 ×2, fresh-DB `alembic upgrade head`+`check` PASS,
  downgrade/upgrade PASS. Остаток: OQ-006 (MFA design) — открыта.
- v8 hardening (2026-09-17): **настоящая server-side ревокация** — `users.token_version`
  (миграция 0003, `ver`-claim, `/auth/logout` + `/auth/users/{id}/revoke-sessions`,
  ADR-013); TD-016 P1 — E1–E4 реализованы (`/operations`, `/operations/daily`,
  `/orders`, `/orders/statuses`, `/reports/payroll-due`; 16 новых тестов, BOLA/paging/401/403);
  TD-017 — drill выполнен реально: pg_dump→checksum→pg_restore (36 таблиц), RPO 24 ч
  (расписание включается вручную), PITR WAL-архив проверен локально (archived 1/0 fail);
  off-site — BLOCKED. pip-audit: 0 уязвимостей (prod+dev). Docker daemon недоступен —
  static review PASS, runtime BLOCKED. Evidence: **313/313 pytest**, ruff 0, mypy 0,
  self-audit 624/624 ×2, alembic upgrade+check+downgrade+upgrade PASS на чистой БД.
  Mutation-проверка: отключение ensure_ip_access → test_multi_ip_isolation ловит (1/1).
- OQ-003 (Kaspi API) — BLOCKED по спецификации; TD-017 — закрыта (off-site = BLOCKED,
  см. docs/BACKUP_DR.md); TD-016 — P1 закрыт, P2/P3/P4/P5 — открыты.

## 2026-09-17 — TD-016 завершён (P1–P5)

- Streamlit → api_client (HTTP) → FastAPI (require_min_role + ensure_ip_access)
  → service/DB. Frontend-скан: 0 прямых DB-вызовов (grep `text(`/`execute(`/`Session`).
- Новый API: E5–E24 + `/ips` (accounts, kaspi/sync-log, import-batches,
  materials, payroll/due, order-costing, ip-summary, work-operation-types,
  audit-log, employees(+q/ON_LEAVE), orders/summary, monitoring/summary,
  auth/users, kaspi/payouts, ip_entities) — пагинация, Decimal-строки, IP-изоляция.
- auth-путь: login/me/logout через API; token_version-ревокация; LOGIN/LOGIN_FAILED
  в audit_log; login-gate тесты переведены на API (TestClient).
- Верификация: 366/366 pytest (фактический счётчик), ruff 0, mypy 0, alembic
  upgrade+check PASS (чистая erp_migration_check), mutation: снятие
  ensure_ip_access_or_all в /operations → FAIL → restore → PASS.

## 2026-09-19 — TD-015 завершён (distributed rate limiting)

- Shared sliding-window в Postgres: `auth_rate_events` (миграция 0004),
  `app/core/rate_limit_store.py`, интеграция в `app/core/security.py`,
  env `RATE_LIMIT_SHARED=1` (production), fail-open-to-local (ADR-014).
- Модель `AuthRateEvent` — drift-тесты (models↔schema↔downgrade) зелёные.
- **Атомарность (проблема из чекпоинта устранена)**: COUNT+INSERT теперь под
  per-bucket transaction-level advisory lock (`lock_bucket`,
  `pg_advisory_xact_lock(hashtextextended(bucket,0))`); INSERT попытки —
  безусловный (prune больше не заменяет запись, а выполняется дополнительно
  с вероятностью 5%).
- **Availability hardening**: контеншн advisory lock не может подвесить login —
  перед `lock_bucket` в сессии limiter `SET LOCAL lock_timeout=600ms` /
  `statement_timeout=700ms` (дефолтный 30s statement_timeout config превышал
  бы fail-open бюджет 0.5 c). Детерминированный тест: удержанный lock в другой
  сессии → limiter деградирует за <5 c в локальное окно.
- Тесты: `test_rate_limit_distributed.py` 7/7, включая **multi-process** тест
  (2 независимых процесса, суммарный бюджет ≤5), fail-open и lock-contention.
- **Concurrency evidence**: 5 независимых прогонов multi-process теста + probe:
  суммарный budget = ровно 5 каждый раз (granted [3,2]/[2,3] — атомарность,
  без пере-гранта до 8).
- **Mutation (TD-015 boundary, на финальном коде)**: снятие проверки
  `seen >= limit` в shared store → multi-process FAIL (`assert 8 <= 5`) +
  прямой shared-only тест FAIL (`DID NOT RAISE`); single-process integration
  test при этом PASS (in-process fast path маскирует — именно поэтому нужен
  multi-process evidence). restore → 7/7 PASS. Удаление advisory lock —
  timing-dependent (3/3 PASS в прогонах) → детерминистичной mutation остаётся
  снятие check. Восстановление через `/tmp/security.py.prod` (md5
  `26e389547511` верифицирован после мутаций).
- **Локализация login gate (единый I18N, RU default/KZ)**: идентификаторы
  пользователя, пароль, кнопка входа, 4 ошибочных сообщения и «Выйти» —
  из каталога `I18N` в `frontend/app.py`; на экране логина есть тот же
  переключатель RU/KZ (key="lang"), выбор сохраняется в сессии. `test_i18n_login.py`
  6/6 (полнота каталога, default RU, чистота рендеров RU и KZ без взаимных
  утечек строк, формат-плейсхолдеры). EN-словарь — отдельный follow-up
  (правка политики §58 упоминает EN; зафиксировано как решение).
- Верификация: **379/379 pytest**, ruff 0, mypy(app) 0, alembic upgrade head
  + check PASS; working tree clean, см. commit.
- Остаток: multi-replica (несколько хостов) — BLOCKED (нет инфраструктуры).

## 2026-09-26 — FINANCE-OPS-001: Transfer ledger

- Scope по FINANCE-OPS-001 §24, REQ-0204/REQ-0229–REQ-0232 и ADR-015: только cross-IP; same-IP transfer не добавлялся.
- `POST /transfers` атомарно создаёт POSTED `Transfer` и две связанные POSTED legs (`TRANSFER_OUT`/`TRANSFER_IN`) с равными противоположными суммами, соответствующими IP/accounts, actor/audit provenance и взаимными `related_operation_id`. Idempotency-Key возвращает прежний aggregate при совпадении payload; конфликт payload — 409; конкурентный повтор не создаёт вторую пару; сбой второй leg откатывает все записи и audit.
- Authorization проверяет доступ к обеим IP до idempotency replay; single-IP ACCOUNTANT не может выполнить cross-IP IDOR. Source/destination account ownership и active-state проверяются. POSTED legs неизменяемы, одиночный reversal запрещён; API DELETE для финансовых операций/transfer отсутствует, DB DELETE guards сохранены.
- `test_finance_integrity.py::test_transfer_excluded_from_pnl` проходит валидный `/transfers` flow и проверяет неизменность P&L summary; transfers остаются вне income, expense и net profit.
- Свежая recovery-проверка PostgreSQL 15.19: до миграции public schema содержала 0 таблиц и не имела `alembic_version`; `alembic upgrade head` прошёл 0001→0006, current=head `0006_transfer_pair_integrity`, ровно один head. В каталоге фактически `convalidated=true` у `chk_transfers_posted_has_legs` и всех трёх transfer/account composite FK. Получено: 32 app tables, 120 app indexes (121 вместе с PK `alembic_version`), 63 triggers, 108 CHECK в public schema, 62 FK, 5 views, 15 application functions, 48 seed rows. Свежая БД не содержала исторических transfers для reconciliation.
- Проверки: `test_transfer_ledger.py` 22/22; `test_finance_integrity.py` 14/14; `test_alembic_baseline.py` 32/32; `test_models_match_schema.py` 81/81; полный PostgreSQL `pytest -q` 416/416; `ruff check .`, `mypy app`, `alembic check`, `git diff --check` и self-audit 652/652 — PASS.
- ADR-015 принят. TD-018 остаётся отдельным незакрытым transfer paired correction/reversal вопросом; по указанию владельца к нему переходить только после Transfer commit.

## 2026-09-26 — FIN-OPS-0001: posted-ledger immutability (non-transfer)

- PHASE 0 discovery (read-only) выявил P0: `fn_protect_posted_operation()` защищает только TRANSFER; POSTED SALE/EXPENSE/SALARY строки изменяемы UPDATE-ом от `erp_app`, включая `REVERSED → POSTED`.
- Добавлены отдельные `fn_protect_posted_ledger_operation()` + `trg_operations_protect_posted_ledger` (schema.sql = SoT, миграция `0007_posted_ledger_immutability`). Transfer guard (ADR-015) не изменён. Данные не мигрируются.
- Сохранено текущее поведение `reverse_operation()` (POSTED → REVERSED, comment) до ADR-016; reversal semantics = BLOCKED (REVERSAL_DECISION_MEMO, Q1–Q7).
- Остаточные изменяемые поля вынесены в TD-019.
- Тесты: `tests/test_ledger_immutability.py` (5 типов × 11 защищённых полей, 7 запрещённых переходов, разрешённые пути, drift schema↔migration, наличие триггера).
- **Статус верификации: НЕ ПРОГНАНО.** Патч подготовлен без PostgreSQL (среда без сети). Gate обязателен перед commit: `scripts/run_gate.sh`.

## 2026-10-01 — TD-019 шаг 2: DELETE-guard provenance-таблиц (autonomous loop)

- `docs/REVERSAL_DECISION_MEMO.md` создан (Q1–Q7, варианты A/B/C, STATUS=BLOCKED — reversal implementation ждёт бизнес-решения; код reversal не тронут).
- TD-019 шаг 2 реализован независимо от ADR-016: `trg_raw_kaspi_payloads_forbid_delete` + `trg_import_batches_forbid_delete` (тот же `fn_forbid_delete()` и migrator-only hatch, TD-003); schema.sql §7.2 = SoT, миграция `0008_provenance_delete_guard` (upgrade/downgrade проверены).
- Порядок purge в `conftest.py` исправлен: children (operations/transfers/kaspi_payouts) → provenance parents (raw_kaspi_payloads/import_batches) → sync_log → ip_entities; SET NULL больше не затирает provenance при purge.
- `scripts/audit_step1.py`: +2 проверки (TD-019 DELETE protection). `tests/test_alembic_baseline.py`: head обновлён на 0008.
- Приложение никогда не DELETE из этих таблиц (проверено поиском по коду) — легитимные flow не затронуты.
- Верификация на PostgreSQL 18.6 (бинарники postgresql-binaries; проект требует 15+): self-audit **656/656**; `alembic upgrade head` → один head `0008_provenance_delete_guard`; downgrade 0008→0007 (триггеры сняты) → upgrade (восстановлены); `alembic check` — no drift; полный pytest **489 passed, 1 skipped** (6 новых тестов; известный flaky `test_multi_worker_budget_is_shared` в этом прогоне прошёл); ruff/mypy/`git diff --check` — PASS.
- Ручная security-проверка: erp_app DELETE → blocked; erp_app + GUC → blocked (TD-003); erp_migrator + GUC → allowed.
- Остаток TD-019: шаг 1 (ADR-016, BLOCKED) и шаг 3 (включить поля в FIN-OPS-001 guard).

## 2026-10-01 — Payroll IP scope: latent cross-IP leak в `payroll_period_totals` (autonomous loop)

- Фаза DISCOVER: проверены старые FIN-OPS findings против текущего кода. `pnl()` корректен (kind+POSTED фильтр, инверсия знака расходов); `calculate_payroll()` корректно скоупирован по ИП; `order_direct_cost()` корректен (frozen purchase price); `reconcile_payout()` корректен (parameterised tolerances); Excel Decimal precision — FALSE POSITIVE (нет кода импорта Excel в бэкенде); i18n baseline RU/KK, EN только для новых строк (не добавлены).
- Найдено: `payroll_period_totals()` (мёртвый код, 0 вызовов) суммировал `work_entries` по всем ИП без фильтра — latent P1 finance correctness issue (cross-IP утечка при будущем вызове).
- Исправлено: добавлен опциональный параметр `ip_ids: list[int] | None` по образцу `calculate_payroll()`; без него поведение не изменилось (0 вызовов — безопасно).
- Тесты: `tests/test_payroll_ip_scope.py` (3 теста: scope к одному ИП, оба ИП, пустой список); mutation check — без фикса 3/3 fail, с фиксом 3/3 pass.
- Верификация на PostgreSQL 18.6 (переустановлен после замены VM; проект требует 15+): 3 новых + 7 существующих payroll-тестов — **10 passed**; ruff — PASS; mypy (24 файла) — PASS; `git diff --check` — PASS.

## 2026-10-02 — ETL edge cases (P1/P1/P2) + i18n sweep A-I (autonomous loop)

- Фаза DISCOVER (2 subagents, вывод проверен главным агентом):
  - ETL edge-case audit (excel-skill pitfalls #1/#5/#10): найдены E-1 (P1), E-2 (P1), E-3 (P2), E-4 (P3 NEXT).
  - i18n completeness sweep: 38 hardcoded строк, группы A–I (~20) исправимы простой заменой; группа J (reports.py, нет lang plumbing) — отдельная задача.
- E-1 (P1): `parse_date` отвергал pandas Timestamp — Excel date-ячейки приходили как datetime64, весь файл уходил в errors. Fix: datetime/Timestamp → .date() до текстового парсинга.
- E-2 (P1): `row_fingerprint` не нормализовал amount — '12500.00' vs '12500' давали разные хеши, нарушая идемпотентность §41 (двойное проведение при повторном импорте). Fix: нормализация к 2 знакам через Decimal.
- E-3 (P2): дублирующиеся заголовки после strip() ('Сумма' vs 'Сумма ') — pandas молча отбрасывал колонку → silent wrong data в ledger. Fix: fail-fast ValueError при неуникальных заголовках.
- E-4 (P3): бухгалтерский формат отрицательных '(12500)' не парсится → error row (fail-safe, знак диктует operation_types.direction §27). NEXT, не исправлено.
- i18n A–I: ~20 строк (module_import, module_banks, module_orders, module_products, module_production, module_payroll, module_settings, kk fallback) перенесены в I18N ru/kk (105 ключей, симметрично); добавлен ключ "sum". module_settings/module_import: `del lang` → `t = I18N[lang]`.
- TDD: 3 новых теста (E-1/E-2/E-3) сначала FAIL, после фикса PASS.
- Верификация на PostgreSQL 18.6 (восстановлен после замены VM; проект требует 15+): полный pytest **496 passed, 1 skipped** (493 + 3 новых); ruff — PASS; mypy — 13 pre-existing ошибок в etl/import_excel.py (не связаны с изменениями); `git diff --check` — PASS.
- Окружение: роли erp_app/erp_migrator пересозданы, таблицы → OWNER erp_migrator (триггеры в тестах), GRANT ALL на таблицы/sequences для erp_app, CREATE ON SCHEMA public.
- Не тронуто: REVERSAL_Q1_Q7, KASPI_OQ_003, REPORT_8_DEFINITION, TD-020 (payroll PAID semantics — BLOCKED, бизнес-решение).

## 2026-10-03 — E-4, i18n J, A-1 (P2), M-1 (P2) (autonomous loop)

- Фаза DISCOVER (4 subagents, вывод проверен главным агентом):
  - E-4: требование не найдено в Master Prompt/матрице/column_map; §27 (знак диктует direction) доказан кодом run_import:218-221. Решение: FIX совместим — `(12500)` → `-12500`, знак всё равно нормализуется через abs().
  - i18n J: reports.py не может импортировать I18N (циклический импорт) → передавать `t` вниз. ~45 ключей × (ru+kk).
  - Audit/provenance: A-1 (P2) — payroll_periods без audit-триггера; A-2/A-3/A-4 (P3 NEXT); F-1..F-4 false positives с доказательством.
  - API↔Frontend: M-1 (P2) — `/employees` игнорировал `?status=`; M-2/M-3 (P3, harmless).
- E-4 (P3): `parse_amount` принимает `(12500)` → `Decimal("-12500")`; `(0)` → ValueError (§27); несбалансированные скобки → ValueError. TDD: тест сначала FAIL, после фикса PASS.
- i18n J (P3): 8 функций reports.py принимают `t`; `_IP_SUMMARY_LABELS` → builder `_ip_summary_labels(t)`; plotly-строки синхронизированы с labels; 3 call sites в app.py обновлены; ~45 новых ключей ru/kk (всего 150, симметрично).
- A-1 (P2): `payroll_periods` добавлен в audit-триггер лист (schema.sql §7.3); миграция `0009_payroll_periods_audit` (upgrade/downgrade); тест `test_payroll_periods_audit_trigger_exists`. Не меняет TD-020 семантику — только audit trail.
- M-1 (P2): `/employees` — добавлен `alias="status"` к `status_filter`, консистентно с `/orders`. Тест `test_employees_status_filter_alias`.
- Верификация: targeted 9 passed; ruff — PASS; `git diff --check` — PASS; mypy — 13 pre-existing (не связаны).
- Pre-existing failures (не связаны с изменениями, падают на 2ba298a): 3× test_td016_p5_endpoints (p5_03, p5_04, p5_07) — test isolation/data setup issue.
- Не тронуто: REVERSAL_Q1_Q7, KASPI_OQ_003, REPORT_8_DEFINITION, TD-020.

## 2026-10-03 — C-1 (P2), R-1..R-4 (P3 docs) (autonomous loop)

- Фаза DISCOVER (2 subagents, вывод проверен главным агентом):
  - Concurrency/idempotency: C-1 (P2) — `add_operation` без идемпотентности на plain API calls (10 concurrent → 10 дубликатов); C-2 (P3) — `add_work_entry` без ключа (mitigated by CONFIRMED review). Transfers/payroll/calculate — CLEAN (empirically proven).
  - Matrix consistency: 9/10 sample verified; R-1..R-4 (P3 stale refs).
- C-1 (P2): POST /operations — добавлен опциональный `Idempotency-Key` header → маппится на `import_fingerprint` (uq_operations_import_fingerprint). Check-first паттерн как в add_transfer: повтор возвращает `{"id": ..., "status": "IDEMPOTENT_REPLAY"}` вместо дубликата. Backward compatible (header optional).
- R-1..R-4 (P3): обновлены stale refs в REQUIREMENTS_MATRIX.md (test count 252→~500, PG 17.11→18.6, REQ-0308 → v_order_costing, FIN-OPS-0001 numbers).
- TDD: `test_post_operation_idempotency_key` — PASS.
- Верификация: ruff — PASS; `git diff --check` — PASS.
- Не тронуто: REVERSAL_Q1_Q7, KASPI_OQ_003, REPORT_8_DEFINITION, TD-020.
