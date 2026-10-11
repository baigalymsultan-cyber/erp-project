# FINAL PROJECT AUDIT (§134, REQ-0334)

Дата: 2026-09-08. Метод: только свежий evidence (команды и их вывод), без
пересказа прошлых отчётов. Каждый аудит завершается вердиктом и открытыми
пунктами (BUG / TD / OQ / DEFERRED — ничего не скрыто, §60, §54).

---

## 1. Requirements Audit

| Статус | Кол-во | Детали |
|---|---|---|
| IMPLEMENTED / TESTED | 66 | `docs/REQUIREMENTS_MATRIX.md`, трассировка REQ → файл → тест |
| BLOCKED | 2 | REQ-0304, REQ-0305 — OQ-003 (нет официальной спецификации Kaspi API; §42/§94 запрещают выдумывать) |
| DEFERRED | 8 | DEF-001..008, каждый с обоснованием (налоги, накладные, склад, мультивалютность, реальные Kaspi endpoint'ы, PDF, банковские выписки, ротация ключей) |
| PENDING | 0 | — |

Команда: `grep -cE "IMPLEMENTED|TESTED" docs/REQUIREMENTS_MATRIX.md`.

**Вердикт: PASSED** — все требования либо закрыты, либо имеют документированную
причину BLOCKED/DEFERRED.

## 2. Security Audit

- Пароли: bcrypt (rounds=12), `chk_users_password_hash` отклоняет plaintext (проверено DDL-аудитом).
- JWT HS256, `APP_JWT_SECRET` из env (§10); секретов в коде нет — `grep -rniE "(password|secret|token)\s*=\s*['\"][A-Za-z0-9]{8,}"` → 0 совпадений.
- RBAC: все endpoint'ы, кроме `/health` (§70 — публичный) и `/auth/login` (по определению), несут `require_privilege(...)`; роли перечитываются из БД на каждом запросе (отзыв роли/блокировка пользователя действуют немедленно).
- Rate limit логина: 5/60 c (`auth_rate_limit`); in-process — корректно при single-process uvicorn, зафиксировано как TD-015 при масштабировании.
- Audit log: маскирование `password_hash`/`encrypted_token` проверено self-audit (624/624).
- Токены Kaspi: `BYTEA`, AES-256-GCM, ключ вне БД (ADR-009).

**Вердикт: PASSED** (открытые: TD-015, DEF-008).

## 3. Database Audit

Evidence (2026-09-08):

```
DROP/CREATE erp_audit → scripts/apply_schema.sh ×2 → RESULT: ALL CHECKS PASSED (624/624, оба прогона)
DROP/CREATE erp_alembic_audit → alembic upgrade head ×2 → OK, 32 базовые таблицы
```

31 бизнес-таблица + alembic_version; 11 функций, 59 триггеров, 116 индексов,
105 CHECK, 60 FK, 3 EXCLUDE, 5 views; seed идемпотентен (двойное применение).

**Вердикт: PASSED** (открытые: TD-001, TD-004, TD-008).

## 4. API Audit

- 17 endpoint'ов (`/operations`, `/transfers`, `/orders`, `/payroll/*`, `/kaspi/*`, `/pnl`, `/auth/*`, `/health`, `/monitoring/summary`).
- Единый `get_db` — транзакция на запрос, commit/rollback (§16); audit user_id из bearer (§54).
- Ошибки: HTTPException с корректными 401/403/404/422; request-лог JSON с request_id (§71).
- OpenAPI автоматически генерируется FastAPI (title/version 0.2.0).

**Вердикт: PASSED.**

## 5. Financial Audit

- 13 обязательных финансовых тестов §67 — `tests/test_finance_integrity.py` (прогон 2026-09-08: PASSED).
- Append-only: DELETE на финансовых таблицах запрещён триггером (`fn_forbid_delete`, escape-hatch только через `app.allow_financial_delete` — проверено self-audit).
- Двойная выплата зарплаты: guard + `SELECT FOR UPDATE` (REQ-0312, тест).
- Reconciliation: 8 классов расхождений (REQ-0307, 13 тестов).
- PnL: только POSTED операции, сторно исключено; kind INCOME/EXPENSE согласован с `v_ip_financial_summary` (TD-014 resolved).
- Прогон 2026-09-08: `pytest tests/test_finance_integrity.py tests/test_payroll_flow.py tests/test_reconciliation.py` → 30 passed.

**Вердикт: PASSED** (открытые: DEF-001 налоги — OQ-001, DEF-002 накладные — OQ-010).

## 6. Testing Audit

```
python -m pytest tests/ → 252 passed in 14.16s (2026-09-08)
ruff check app tests → All checks passed!
mypy app → Success: no issues found in 22 source files
```

Слои: unit / integration / api (реальный ASGI+PostgreSQL) / db (триггеры,
EXCLUDE, GENERATED) / etl / e2e smoke (Streamlit AppTest). Тесты на реальном
PostgreSQL — SQLite не используется (моки не выдаются за proof, §54).

**Вердикт: PASSED.**

## 7. Performance Audit

- 116 индексов в DDL; pagination (`limit/offset`) на list-endpoint'ах; пул SQLAlchemy (`create_database_engine`); батчи в ETL; P&L — SQL-агрегация (view), не Python.
- Осознанные лимиты: без партиционирования до ~5 млн строк (TD-001), view не материализован (TD-007) — пороги и план зафиксированы.

**Вердикт: PASSED** для стартовой нагрузки (обоснование в TD).

## 8. Deployment Audit

- `docker/Dockerfile`: python:3.12-slim, non-root (uid 10001), healthcheck `/health`, CMD выполняет `alembic upgrade head` перед uvicorn (исправлено по baseline-audit §55, п.2).
- `docker/docker-compose.yml`: db только `expose: 5432` внутри compose-сети, без хост-порта (исправлено, ADR-005); секреты из `.env` (§10), не в образе.
- Ограничение: в текущей среде нет Docker-демона — сборка образа не выполнена; синтаксис compose/Dockerfile проверен ревизией, миграции на чистой БД проверены напрямую (`ALEMBIC_OK`, 32 таблицы). Полная `docker compose up --build` — при первом деплое (зафиксировано ниже).
- Rollback: `docs/ROLLBACK_PLAN.md` (REQ-0333).

**Вердикт: PASSED WITH NOTE** — Docker-сборка не запускалась в среде разработки
(нет демона); отмечено как обязательный шаг при первом реальном деплое.

## 9. Backup Audit

- `scripts/backup.sh` — pg_dump + sha256; `scripts/restore.sh` — восстановление (проверено psql-путём `postgres`).
- Restore-тест: `tests/test_backup_restore.py` — round-trip дампа в scratch-БД, сравнение строк 12 бизнес-таблиц + контрольная сумма (прогон в suite 252).
- Расписание + retention — включаются командой пользователя (REQ-0326, OQ: целевой environment).

**Вердикт: PASSED** (расписание — по команде).

## 10. Documentation Audit

- `docs/README.md` — обновлён (§55 п.4/п.13 documentation drift устранён: статус ШАГ 16, 252/252, актуальная матрица).
- `docs/PROJECT_PLAN.md`, `docs/PROGRESS.md`, `docs/REQUIREMENTS_MATRIX.md`, `docs/TECHNICAL_DEBT.md`, `docs/OPEN_QUESTIONS.md`, `docs/ADR.md`, `docs/ROLLBACK_PLAN.md` — согласованы между собой.
- Ответы на 15 open questions не выдуманы (§94); 2 из них блокируют Kaspi-интеграцию.

**Вердикт: PASSED.**

---

## Сводка

| Аудит | Вердикт |
|---|---|
| Requirements | PASSED |
| Security | PASSED (TD-015, DEF-008 открыты) |
| Database | PASSED |
| API | PASSED |
| Financial | PASSED |
| Testing | PASSED — 252/252 |
| Performance | PASSED (пороги в TD-001/007) |
| Deployment | PASSED WITH NOTE (нет Docker-демона в среде) |
| Backup | PASSED |
| Documentation | PASSED |

Production-Ready Gate (§51): application/migration/security/authorization
verification — есть; backup/restore/monitoring/rollback — есть; **нет**:
реальный деплой, alerting-канал, dependency security scan (pip-audit), DR-план
вне restore, business/legal acceptance, реальные Kaspi-вызовы (OQ-003).
Проект **не объявляется production-ready** до закрытия этих пунктов (§51).
