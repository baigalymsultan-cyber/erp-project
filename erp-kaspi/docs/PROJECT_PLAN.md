# PROJECT ROADMAP — PLAN v1.0

Проект: **Production ERP / Finance / Kaspi / MRP (Kazakhstan)**
Дата плана: 2026-08-28
Версия: PLAN v1.0
Источник требований: MASTER PROMPT v3.0 (§0–135)

---

## 1. АРХИТЕКТУРНОЕ РЕЗЮМЕ

| Слой | Технология | Обоснование |
|---|---|---|
| Язык | Python 3.11+ | MASTER PROMPT §7, §8 (Python-first) |
| Backend API | FastAPI | §7 |
| БД | PostgreSQL 15+ (проверено на 17.11) | §7, §13 |
| ORM | SQLAlchemy 2.x | §7 |
| Миграции | Alembic | §7, §65 |
| Валидация | Pydantic v2 | §7 |
| HTTP-клиент | httpx + Tenacity | §7, §43 |
| ETL | Pandas | §7, §39 |
| Крипто | cryptography (AES-256-GCM) | §7, §19 |
| Тесты | pytest | §7, §66 |
| UI | Streamlit + Plotly | §7, §58 |
| Контейнеры | Docker + docker-compose | §7, §68 |
| CI/CD | GitHub Actions | §7, §69 |

Node.js / React / Vue / Angular / TypeScript **не используются** (§8).

## 2. КРИТИЧЕСКИЙ ПУТЬ (§103)

```
DATABASE SCHEMA
   ↓
DOMAIN MODELS (SQLAlchemy) + MIGRATIONS
   ↓
DOMAIN SERVICES (finance / production / payroll)
   ↓
INTEGRATIONS (Kaspi Shop / Kaspi Pay)  ──→  RECONCILIATION
   ↓
AUTH + RBAC + AUDIT
   ↓
FastAPI
   ↓
STREAMLIT UI
   ↓
TESTS → DOCKER → CI/CD → DEPLOYMENT → HARDENING
```

Блокирующая зависимость вне контроля проекта: **официальная спецификация Kaspi API**
(§42, §94). До её получения ШАГ 3/ШАГ 4 остаются частично `BLOCKED`.

## 3. MILESTONES (§115)

| ID | Milestone | Acceptance Criteria (§116) |
|---|---|---|
| M1 | Architecture | PLAN v1.0, Requirements Matrix, ADR-001..006, Open Questions зафиксированы |
| M2 | Database | `database/schema.sql` применяется на чистом PostgreSQL без ошибок; 17 исходных таблиц + расширения; все CHECK/FK/UNIQUE/индексы/триггеры/views/seed присутствуют; self-audit скрипт зелёный; повторный запуск seed не создаёт дублей |
| M3 | ETL | Excel-импорт с `--dry-run`, идемпотентность, отчёт по ошибкам, тесты |
| M4 | Kaspi | KaspiShopClient / KaspiPayClient с retry, timeout, raw payload, idempotency |
| M5 | Reconciliation | Order ↔ Payout ↔ Operation сверка, детект 8 типов расхождений |
| M6 | Production | Products/Materials/WorkEntries/Costing + rate & price freeze тесты |
| M7 | Payroll | Периоды, summary, выплаты, связь с operations, тесты |
| M8 | Security | Auth, RBAC на backend, audit log, шифрование токенов, security review |
| M9 | API | FastAPI роуты, схемы Pydantic, health, OpenAPI |
| M10 | Frontend | Streamlit dashboard + 10 модулей sidebar, RU/KK |
| M11 | Testing | unit/integration/api/db/etl/e2e; 13 обязательных финансовых тестов (§67) |
| M12 | Docker | Dockerfile + docker-compose (app + postgres) поднимается |
| M13 | CI/CD | Pipeline: lint → typecheck → tests → build → migration check |
| M14 | Deployment | Staging-деплой, smoke test, rollback-план |
| M15 | Production Hardening | Backup + restore test, monitoring, final audit (§134) |

После каждого milestone — **STOP** и ожидание команды пользователя (§117).

## 4. ПЛАН НА 14 ДНЕЙ (§99, §100)

Рабочий день = 6 часов продуктивного времени, 25% буфер (§105).

### DAY 1 — Architecture & Database Schema  ← ТЕКУЩИЙ ДЕНЬ
- **Goal**: M1 + M2. Полный рабочий PostgreSQL DDL.
- **Tasks**: TASK-001..TASK-012 (планирование, матрица требований, ADR, DDL, seed, views, триггеры, self-audit).
- **Dependencies**: нет.
- **Expected Result**: `database/schema.sql` применён на реальном PostgreSQL 17; self-audit пройден.
- **Tests**: `scripts/audit_step1.py` — проверка 17 таблиц, колонок, типов, NOT NULL, DEFAULT, PK/FK/UNIQUE/CHECK, индексов, generated-колонок, триггеров, views, seed; повторный прогон seed.
- **Risks**: неоднозначность формул (§35, §76) — MEDIUM; решено через ASM-записи.
- **Acceptance**: DDL применяется дважды без ошибок; 0 MISSING REQUIREMENT.

### DAY 2 — SQLAlchemy Models + Alembic Baseline
- Goal: доменные модели 1:1 со схемой, Alembic baseline-миграция, автотест «модели == схема».
- Depends: DAY 1. Complexity: HIGH. Risk: MEDIUM.
- Tests: миграция up/down на чистой БД; сравнение metadata с DDL.

### DAY 3 — Core: config, security, crypto, logging, audit context
- Goal: `app/core` — settings (Pydantic Settings), AES-256-GCM шифрование токенов + rotation, structured logging без секретов, контекст пользователя для audit-триггеров.
- Depends: DAY 2. Complexity: MEDIUM. Risk: HIGH (крипто).
- Tests: encrypt/decrypt/rotate; проверка что plaintext-токен не логируется.

### DAY 4–5 — ШАГ 2: Excel ETL + Dry Run + Idempotency
- Goal: `etl/` — чтение Excel (Pandas), нормализация дат/чисел/пустых ячеек, валидация, unknown-справочники, `--dry-run`, отчёт (inserted/skipped/duplicates/errors), import_batches, фингерпринты.
- Depends: DAY 3. Complexity: HIGH. Risk: HIGH (структура файла неизвестна → OQ-002).
- Tests: повторный импорт не создаёт дублей; dry-run не пишет в БД; битые строки.

### DAY 6 — ШАГ 3: Kaspi API Clients (skeleton + spec research)
- Goal: KaspiShopClient / KaspiPayClient: auth, timeout, retry (Tenacity, exp. backoff), rate limiting, pagination, raw payload storage. Реальные endpoint'ы — только после проверки официальной документации (§42, §94).
- Depends: DAY 3. Complexity: HIGH. Risk: CRITICAL (BLOCKED — EXTERNAL API SPECIFICATION REQUIRED).
- Tests: mock-транспорт, retry не создаёт повторную финансовую операцию.

### DAY 7 — WEEKLY REVIEW #1 (§111) + ШАГ 4 начало: Kaspi Sync
- Goal: sync_log, идемпотентная загрузка заказов/выплат, ревью прогресса и архитектуры.
- Depends: DAY 6.

### DAY 8 — ШАГ 4: Reconciliation
- Goal: сверка Order ↔ Payout ↔ Operation; детект duplicate / missing / amount mismatch / commission mismatch / delivery mismatch / refund / partial payout / unexplained payment.
- Depends: DAY 7. Complexity: CRITICAL. Risk: HIGH.
- Tests: обязательные финансовые тесты §67 (частично).

### DAY 9 — ШАГ 5–6: Domain services, Production, Costing
- Goal: operations/transfers сервисы (atomic, idempotent), work entries, rate freeze, material price freeze, order costing (Material + Labor → Gross Profit → Margin).
- Depends: DAY 8. Complexity: HIGH.

### DAY 10 — ШАГ 7: Payroll
- Goal: периоды, piecework агрегация, оклад/смешанная оплата, summary, выплаты → operations (SALARY_PAYMENT).
- Depends: DAY 9. Complexity: HIGH. Risk: HIGH (правила зарплаты — OQ-007).

### DAY 11 — ШАГ 8: Auth + RBAC + Audit
- Goal: users/roles, secure password hash, session/token security, backend-проверка прав по 7 ролям, audit_log покрытие.
- Depends: DAY 10. Complexity: HIGH. Risk: HIGH.

### DAY 12 — ШАГ 9: FastAPI
- Goal: роуты по домену, Pydantic-схемы, health endpoint, pagination, error handling.
- Depends: DAY 11.

### DAY 13 — ШАГ 10–11: Streamlit + Reports
- Goal: dashboard (оборот/выручка/расходы/прибыль/зарплата/себестоимость), фильтры периодов, 10 модулей sidebar, 8 отчётов, экспорт Excel.
- Depends: DAY 12. Complexity: HIGH.

### DAY 14 — ШАГ 12–13: Testing + Docker
- Goal: полный прогон тестов (§66, §67), Dockerfile, docker-compose, coverage-отчёт.
- Depends: DAY 13.

**За пределами 14 дней (отдельный план v2.0)**: ШАГ 14 CI/CD, ШАГ 15 Deployment,
ШАГ 16 Monitoring + Backup + Hardening. Планировать после WEEKLY REVIEW #2 (§111).

## 5. РИСКИ

| ID | Риск | Вероятность | Влияние | Митигация |
|---|---|---|---|---|
| R-01 | Kaspi API spec недоступна | HIGH | CRITICAL | Абстракция клиента + raw payload; пометка BLOCKED, не выдумывать API (§94) |
| R-02 | Структура Excel неизвестна | HIGH | HIGH | Конфигурируемый маппинг колонок; dry-run |
| R-03 | Правила payout/комиссий Kaspi | HIGH | CRITICAL | Reconciliation на данных, а не на предположениях; OQ-004 |
| R-04 | Налоговый режим ИП | MEDIUM | HIGH | tax_rates_history вместо константы (§75); OQ-001 |
| R-05 | Двойная выплата / дубли импорта | MEDIUM | CRITICAL | UNIQUE + транзакции + SELECT FOR UPDATE (§57) |
| R-06 | Потеря истории при правках | MEDIUM | CRITICAL | Запрет DELETE на финансовых таблицах, reversal-модель (§16) |
| R-07 | Платформа не поддерживает стек в проде | CERTAIN | MEDIUM | ADR-006: внешний хостинг; разработка в песочнице |
| R-08 | Утечка токенов/ключей | LOW | CRITICAL | AES-256-GCM, ключ вне БД, запрет логирования (§19, §71) |

## 6. НЕИЗВЕСТНЫЕ

Ведутся в `docs/OPEN_QUESTIONS.md` (§95). Не выдумываются (§94).

## 7. МЕТРИКИ ПРОГРЕССА (§114)

Обновляются в конце каждого дня в `docs/PROGRESS.md`.

## 8. ПРАВИЛО ПЕРЕХОДА (§79, §117)

Выполняется **только** запрошенный пользователем шаг. Автоматического перехода
к следующему milestone нет.
