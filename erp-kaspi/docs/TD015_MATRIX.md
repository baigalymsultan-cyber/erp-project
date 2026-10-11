# TD-015 — Distributed rate limiting — Requirements Matrix (2026-09-18)

| ID | Требование | Решение | Проверка |
|---|---|---|---|
| TD015-01 | Single-process поведение | Существующий in-process deque заменён на DB-backed окно; поведение (5/60с) сохранено | unit-тесты |
| TD015-02 | Multi-worker behavior | Состояние в таблице `auth_rate_events` (Postgres) — общее для всех процессов одного экземпляра | test_multi_worker_budget_is_shared (2 реальных процесса) |
| TD015-03 | Multi-replica behavior | Та же таблица, БД — общий backend для реплик | та же механика, что 02; full replica-кластер = BLOCKED (нет инфраструктуры) |
| TD015-04 | Atomic increment/check | `INSERT` + `SELECT count(*)` в одной транзакции на уровне БД; сериализация пер-bucket через `pg_advisory_xact_lock(hashtextextended(bucket,0))`; каждая попытка записывается до 429 | test_shared_only_path_rejects_sixth_and_records_every_attempt + concurrency |
| TD015-05 | Fail-open/fail-closed | Деградация: при недоступности/таймауте (0.5 c, 3 retry) shared store — продолжаем считать в per-worker окне того же размера (5/60 c); недоступность логируется | failure-тест (SQLAlchemyError → окно продолжает работать) |
| TD015-06 | Availability impact | +1 короткий INSERT+COUNT на auth-попытку при RATE_LIMIT_SHARED=1; prune вероятностный (5%) вместо каждого INSERT; контеншн lock ограничен `SET LOCAL lock_timeout=600ms`/`statement_timeout=700ms` | perf-наблюдение + test_shared_lock_contention_fails_open |
| TD015-07 | Auth endpoint protection | `/auth/login` (ip:username) и `/auth/password-change` (pwchange:ip:uid) — оба через limiter | существующие 429-тесты |
| TD015-08 | IP + username dimensions | Окно `ip:username` 5/60 c (каждая попытка); username-rotation ограничен per-worker окном при деградации и audit-мониторингом failed_logins; отдельный per-IP лимит 30/60 c — DEFERRED (не блокирует §55) | тесты + TD-запись |
| TD015-09 | Memory/resource exhaustion | Очистка старых событий: opportunistic DELETE (2×window, вероятность 5%) в транзакции записи | тест очистки (prune) |
| TD015-10 | Retry/timeout | Shared-вызов ограничен дедлайном 0.5 c (3 retry с backoff 50/100 мс); при истечении — деградация в per-worker окно; недоступность логируется | failure-тест (mock) |
| TD015-11 | Configuration | Окно/максимум — константы модуля §55; shared режим — env `RATE_LIMIT_SHARED=1` (prod compose), dev/CI без внешней инфраструктуры | код-ревью |
| TD015-12 | Secrets | Новых секретов нет — используется существующий DSN | — |
| TD015-13 | Tests | unit + 429 + multi-process + failure-mode + mutation | pytest |
| TD015-14 | Observability | 429 уже пишется в audit_log (LOGIN_FAILED); rejected попытки видны | существующий тест |
| TD015-15 | Documentation | ADR-014 + PROGRESS + TECHNICAL_DEBT обновлены | docs |
| TD015-16 | Backward compatibility | API `auth_rate_limit()` вызывается из 2 мест; сигнатура меняется координированно; reset-хелпер сохранён | regression |
