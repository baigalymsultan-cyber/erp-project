# TD-016 — Streamlit read-SQL → FastAPI/service layer

**Статус: INVENTORY + REQUIREMENTS MATRIX готовы (2026-09-16). Имплементация не начата.**
Метод: inspect → inventory → mapping → matrix → migration order. Никакой код frontend
и API на этом этапе не изменён (требование поэтапности).

## 1. Текущая архитектура доступа к БД из UI

| Слой | Что делает |
|---|---|
| `frontend/app.py:267` `engine()` | `create_engine(DatabaseSettings.from_env().dsn())` — прямое подключение БД |
| `frontend/app.py::main` | на каждый рендер: `with engine().connect() as conn:` → сырой SQL |
| `frontend/auth_gate.py::ip_guard` | изоляция ИП в WHERE: `FALSE` / `TRUE` / `id IN (...)` через `app.core.policy.allowed_ip_ids` |
| `frontend/auth_gate.py::authenticate` | прямые чтение/запись: `users` SELECT, `failed_logins`/`locked_until`/`last_login_at` UPDATE, `audit_log` INSERT |
| `frontend/app.py::foreman_form` (save) | ЕДИНСТВЕННЫЙ write через service layer: `add_work_entry()` (`app.services`) + read-back `rate_at_time/total_sum` |

API-слой (`app/api/__init__.py`) авторизуется через `require_privilege` +
`app.core.policy.ensure_ip_access*` — та же политика (`app/core/policy.py`), что и UI.
Это значит: перенос чтений в API не теряет изоляцию ИП, при условии что каждый
новый list-endpoint обязан вызывать `ensure_ip_access_or_all` (см. REQ-TD016-02).

## 2. Инвентаризация всех DB-обращений frontend

### frontend/app.py (модули sidebar, §60)

| # | Функция (строка) | Таблица/VIEW | Операция | ip_guard |
|---|---|---|---|---|
| A1 | `paged_table` (L306–335) | generic (COUNT + SELECT LIMIT/OFFSET) | READ ×N | в переданном SQL |
| A2 | `pnl_dataframe` (L402) | `operations ⋈ operation_types` — дневные income/expense, status=POSTED | READ | нет (период, не ИП) |
| A3 | `foreman_form` (L432) | `employees` (ACTIVE) | READ | да |
| A4 | `foreman_form` (L443) | `work_operation_types` (is_active) | READ | нет |
| A5 | `foreman_form` (L453) | `orders` (последние 200, не CANCELLED) | READ | да |
| A6 | `foreman_form` (L465) | `ip_entities` | READ | да |
| A7 | `foreman_form` save (L522) | `work_entries` (read-back ставки) | READ | по созданной записи |
| A8 | save-путь (L517) | `add_work_entry` (service layer) | WRITE | service layer |
| A9 | `module_operations` (L556) | `ip_entities` | READ | restrict_ip_choices |
| A10 | `module_operations` (L584/599/608) | `operations ⋈ types ⋈ ip ⋈ counterparties` KPI+count+rows | READ | да |
| A11 | `module_import` (L627/645) | `import_batches` KPI+rows | READ | нет (системные) |
| A12 | `module_banks` (L661) | `accounts ⋈ ip_entities` | READ | да |
| A13 | `module_banks` (L679) | `kaspi_payouts ⋈ ip_entities` | READ | да |
| A14 | `module_banks` (L702) | `sync_log ⋈ ip_entities` | READ | да |
| A15 | `module_orders` (L729/744/749/758) | `orders` статусы/KPI/by_status/rows | READ | да |
| A16 | `module_products` (L838/848) | `products` KPI+rows | READ | нет |
| A17 | `module_production` (L893) | `work_entries` KPI | READ | да |
| A18 | `module_production` (L902/913/919) | `v_work_entries_detail` список | READ | да |
| A19 | `module_payroll` (L949) | `v_payroll_due` KPI | READ | да |

### frontend/reports.py (render_reports)

| # | Функция | VIEW | Операция | ip_guard |
|---|---|---|---|---|
| B1 | `_ip_summary` (L77) | `v_ip_financial_summary` | READ | да |
| B2 | `_kaspi_reconciliation` (L124) | `v_kaspi_reconciliation` | READ | да |
| B3 | `_payroll` (L146) | `v_payroll_due` | READ | да |
| B4 | `_order_costing` (L174) | `v_order_costing` | READ | да |
| B5 | `_operations` (L196/219) | `operations` (отображение + сырые суммы) | READ | да |
| B6 | `_materials` (L244/264) | материалы (display + raw) | READ | да |
| B7 | `_work_entries` (L285) | `v_work_entries_detail` | READ | да |

### frontend/admin_modules.py

| # | Функция | Таблица | Операция | ip_guard |
|---|---|---|---|---|
| C1 | `module_employees` (L47/67) | `ip_entities`, `employees` KPI+rows | READ | да |
| C2 | `module_monitoring` (L99–117) | `to_regclass`×6, `sync_log` agg 24h, `users.failed_logins` | READ | нет |
| C3 | `module_monitoring` (L139) | последние sync_log | READ | нет |
| C4 | `module_users` (L162/195) | `users`, `roles` (матрица) | READ | нет |
| C5 | `module_audit` (L162/216–246) | `audit_log` фильтры + rows | READ | нет |

### frontend/auth_gate.py (write-путь — особый случай)

| # | Функция | Таблица | Операция |
|---|---|---|---|
| D1 | `authenticate` (L119) | `users` SELECT + `verify_password` | READ |
| D2 | `_record_failed_login` (L78) | `users` failed_logins/locked_until | WRITE |
| D3 | `_audit_standalone` (L107) | `audit_log` LOGIN/LOGIN_FAILED | WRITE |
| D4 | `authenticate` success (L154) | `users` last_login_at, failed_logins=0 | WRITE |
| D5 | `ip_guard` (L37) | `SET LOCAL` GUC-контекст | CONTEXT |

## 3. Сопоставление с существующими API endpoints

| Endpoint (существует) | Покрывает UI-потребность | Вердикт |
|---|---|---|
| `GET /health`, `GET /monitoring/summary` | C2/C3 (мониторинг) | ПОЛНОЕ покрытие — UI дублирует API своим SQL |
| `GET /pnl` | A2 (дневной ряд) | ЧАСТИЧНОЕ: API возвращает агрегат за период, UI нужен дневной ряд |
| `GET /payroll/summary?employee_id` | B3 (v_payroll_due) | НЕ ЭКВИВАЛЕНТ: API — по сотруднику из `payroll_summary`, UI — сводка по ИП из view |
| `POST /payroll/work-entries` | A8 (создание выработки) | ПОЛНОЕ: тот же `add_work_entry`; UI пишет через service напрямую |
| `GET /kaspi/payouts` | A13 | ПОЛНОЕ (список есть; UI фильтрует по ИП и лимитирует 100) |
| `POST /auth/login`, `GET /auth/me` | D1–D5 | ЛОГИЧЕСКОЕ покрытие есть; UI намеренно дублирует семантику локально (TD-014) |
| `GET /operations/{id}`, `POST /operations` | A10/B5 (список с фильтрами) | НЕТ list-endpoint с фильтрами/пагинацией |
| остальные A/B/C | — | НЕ ПОКРЫТО — endpoints отсутствуют |

## 4. Недостающие endpoints (к созданию в фазе имплементации)

Все обязаны: JWT `require_privilege` ≥ VIEWER (или роли из §53), изоляция ИП через
`ensure_ip_access_or_all`, пагинация LIMIT/OFFSET, `response_model`.

| # | Endpoint (предложение) | Заменяет | Приоритет |
|---|---|---|---|
| E1 | `GET /operations` (фильтры: период, kind, ip_id, поиск; paging) | A10, B5 | P1 |
| E2 | `GET /operations/daily` (или расширение `GET /pnl` параметром `daily=true`) | A2 | P1 |
| E3 | `GET /orders` (status, поиск, paging) + `GET /orders/statuses` | A15 | P1 |
| E4 | `GET /reports/payroll-due` (v_payroll_due, сводка по ИП) | B3, A19 | P1 |
| E5 | `GET /reports/ip-summary` | B1 | P2 |
| E6 | `GET /reports/kaspi-reconciliation` | B2 | P2 |
| E7 | `GET /reports/order-costing` | B4 | P2 |
| E8 | `GET /reports/work-entries` (v_work_entries_detail) | B7, A18 | P2 |
| E9 | `GET /products` (поиск, active, paging) | A16 | P2 |
| E10 | `GET /materials` | B6 | P2 |
| E11 | `GET /accounts` | A12 | P2 |
| E12 | `GET /kaspi/sync-log` | A14 | P2 |
| E13 | `GET /import-batches` (KPI + paging) | A11 | P2 |
| E14 | `GET /employees` (справочник + реестр HR) | A3, C1 | P3 |
| E15 | `GET /work-operation-types` (active) | A4 | P3 |
| E16 | `GET /audit-log` (фильтры entity/action/user + paging) | C5 | P3 |
| E17 | `GET /auth/users` (список + матрица ролей; ADMIN) | C4 | P3 |

Особые случаи:
- **D1–D5 (auth)** — НЕ механический перенос. Дизайн-решение (OQ-TD016-01):
  сохранять локальный login-gate UI (текущее поведение TD-014, идентичная семантика
  API) либо звонить в `POST /auth/login` по HTTP и хранить JWT в session_state.
  Решение требует security review; в inventory фиксируется как открытое.
- **A7 (read-back ставки)** — уходит автоматически: `POST /payroll/work-entries`
  уже возвращает созданную запись (проверить response; при необходимости расширить).
- **A1 `paged_table`** — остаётся как UI-хелпер, но SQL в него больше не передаётся;
  данные приходят из endpoints E1–E17.

## 5. Requirements Matrix TD-016

| REQ-ID | Требование | Acceptance criteria | Заменяет |
|---|---|---|---|
| REQ-TD016-01 | Каждый list-endpoint из §4 авторизуется JWT (`require_privilege`) | отрицательный тест: без токена → 401 на все E1–E17 | — |
| REQ-TD016-02 | Изоляция ИП на сервере: `ensure_ip_access_or_all` в каждом endpoint с данными по ИП (E1–E14, E16) | тест: пользователь с привязкой к ИП-1 не видит строки ИП-2 (API1/BOLA) | ip_guard в UI |
| REQ-TD016-03 | Пагинация LIMIT/OFFSET + total на сервере; UI не режет данные молча | тест: total совпадает, страницы корректны | A1 |
| REQ-TD016-04 | UI не содержит `create_engine`/`text()` для данных; данные — только через httpx → API | grep-аудит frontend: 0 вхождений `conn.execute(text(` для бизнес-данных (кроме auth-пути до решения OQ-TD016-01) | все A/B/C |
| REQ-TD016-05 | Response models для всех новых endpoints; суммы — строки (Decimal-safe) | mypy 0; схема ответа зафиксирована | — |
| REQ-TD016-06 | Роли UI (`ADMIN_MODULE_ROLES`) соблюдаются на сервере: E16 → DIRECTOR+, E17 → ADMIN+ | тест: VIEWER → 403 на E16/E17 | §53 |
| REQ-TD016-07 | Пирамидные KPI UI (income/expense/profit, суммы импортов, payroll due) считаются теми же SQL-агрегатами, что сегодня, — расхождений нет | сравнительный тест: значения UI до/после на одной БД идентичны | A10/A11/A19 |
| REQ-TD016-08 | Дневной ряд P&L соответствует текущему `pnl_dataframe` (POSTED-only, знаки §27) | golden-тест на фикстурной БД | A2 |
| REQ-TD016-09 | Аудит-логирование операций сохраняется (§54): login-путь UI не теряет LOGIN/LOGIN_FAILED записи | тест: попытки логина пишут audit_log | D3 |
| REQ-TD016-10 | Тесты: для каждого нового endpoint — позитивный + негативный (401/403/BOLA) тест; полный прогон 280+ зелёный | pytest PASS, ruff 0, mypy 0 | — |
| REQ-TD016-11 | Никакой рефакторинг вне скоупа TD-016 (не трогаем schema.sql, services, etl) | diff-аудит коммита | — |

Открытый вопрос: **OQ-TD016-01** (auth-путь UI: локальный gate vs HTTP login) —
решается до фазы D; влияет только на auth_gate.py, не на остальные модули.

## 6. Порядок миграции (имплементация — отдельные этапы, не сейчас)

1. **P1 — финансовое ядро чтений**: E1, E2, E3, E4 + UI-переключение операций/заказов/payroll-due.
2. **P2 — отчёты и справочники**: E5–E13 + переключение отчётов/банков/импорта/товаров/материалов.
3. **P3 — админ-модули и форейман**: E14–E17 (+ переключение на существующий `/monitoring/summary`).
4. **P4 — решение OQ-TD016-01 (auth-путь)** — security review, потом перенос/оставление.
5. **P5 — зачистка**: удалить мёртвый SQL из frontend, grep-аудит REQ-TD016-04, полный regression.

Каждая фаза: implement → test → security audit → regression → evidence → commit.

## Статус (2026-09-17) — РЕАЛИЗОВАНО

- P1 (E1–E4): PASS. P2 (E5–E17): PASS. P3 (mut-тесты): PASS.
- P4 (auth-путь на api_client; OQ-TD016-01): PASS.
- P5 (app.py/reports.py/admin_modules.py на api_client): PASS.
- Дополнительные endpoints сверх плана: E18–E24 + GET /ips (см. PROGRESS).
- Evidence: 366/366 pytest; ruff 0; mypy 0; alembic check PASS (чистая БД);
  frontend grep-скан: 0 прямых DB-вызовов; mutation
  (ensure_ip_access_or_all → FAIL → restore → PASS).
