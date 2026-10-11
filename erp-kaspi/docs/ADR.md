# ARCHITECTURAL DECISION RECORDS (§126)

---

## ADR-001 — PostgreSQL как основная СУБД

**Статус**: ACCEPTED
**Контекст**: Финансовая система с требованиями к целостности, транзакциям,
generated-колонкам, exclusion-констрейнтам, JSONB для raw API payload, NUMERIC для денег.
**Решение**: PostgreSQL 15+ (проверено на 17.11).
**Обоснование**:
- `NUMERIC(15,2)` — точная денежная арифметика без float-ошибок.
- `TIMESTAMPTZ` — корректное хранение UTC (§14).
- `JSONB` — raw Kaspi payload с возможностью индексации (§26).
- `GENERATED ALWAYS AS ... STORED` — `work_entries.total_sum` считается СУБД (§33).
- `EXCLUDE USING gist` + `btree_gist` — запрет пересечения периодов ставок (§31) на уровне БД,
  а не приложения.
- Транзакции + `SELECT FOR UPDATE` — защита от двойных выплат (§57).

**Альтернативы отклонены**: SQLite (нет TIMESTAMPTZ, нет EXCLUDE, слабые типы, не подходит
для конкурентной финансовой записи), MySQL (слабее JSON и exclusion-констрейнты).

---

## ADR-002 — FastAPI как backend-фреймворк

**Статус**: ACCEPTED
**Решение**: FastAPI + Pydantic v2 + SQLAlchemy 2.x.
**Обоснование**: нативная валидация запросов через Pydantic (§7), автогенерируемый OpenAPI,
поддержка async для httpx-интеграций с Kaspi, dependency injection для RBAC-проверок на
backend (§53).

---

## ADR-003 — Streamlit как frontend

**Статус**: ACCEPTED
**Решение**: Streamlit + Plotly (§7, §58).
**Обоснование**: внутренняя админ-система для небольшого числа пользователей; скорость
разработки форм ввода (бригадир вводит work entries) важнее кастомного UX. Python-only —
нет второго языка и второго процесса сборки (§8).
**Ограничение**: Streamlit — не публичный SPA. При необходимости внешнего портала — отдельное
архитектурное решение (§8).

---

## ADR-004 — Supabase не является обязательным

**Статус**: ACCEPTED
**Решение**: используется собственный PostgreSQL; Supabase не требуется.
**Обоснование**: проекту нужны миграции Alembic, произвольный DDL (EXCLUDE-констрейнты,
триггеры, generated-колонки), полный контроль над транзакциями и бэкапами. Supabase добавил бы
внешнюю зависимость, RLS-модель, дублирующую RBAC приложения, и сетевую задержку без
компенсирующей выгоды. Финансовые данные остаются под прямым контролем.
**Последствие**: бэкапы и мониторинг БД — ответственность проекта (§73, §72).

---

## ADR-005 — PostgreSQL не exposed to Internet

**Статус**: ACCEPTED
**Решение**: порт PostgreSQL не публикуется в интернет. Доступ — только из внутренней сети
приложения (docker network) либо через SSH-туннель/VPN.
**Обоснование**: прямой доступ к финансовой БД из интернета — недопустимая поверхность атаки
(brute force, утечка credentials, эксплойты протокола). В `docker-compose.yml` сервис
`postgres` не имеет публичного `ports:`-маппинга в production-профиле.
**Последствия**: администрирование — через bastion/SSH; для локальной разработки допускается
маппинг на `127.0.0.1`.

---

## ADR-006 — Хостинг вне текущей платформы (Cloudflare-ограничения)

**Статус**: ACCEPTED
**Контекст**: Требуемый стек — Python/FastAPI/PostgreSQL/Streamlit/Docker. Платформа
разработки деплоит только Cloudflare Workers/Pages: нет Python-рантайма, нет серверных
процессов, нет PostgreSQL (только D1/SQLite и R2), нет cron-триггеров и Docker.
**Решение**: стек **не меняется** (§8 требует Python-first). Продакшен-хостинг —
внешний: VPS/Docker-хост либо managed Postgres (Neon/Supabase-as-Postgres) + PaaS для
FastAPI/Streamlit. Песочница использукак dev/CI-среда.
**Отклонено**: миграция на Cloudflare D1 + TypeScript. Причина: SQLite не даёт
`NUMERIC`, `TIMESTAMPTZ`, `EXCLUDE`-констрейнтов, `SELECT FOR UPDATE`; это прямо ломает
требования §13, §14, §31, §57 к финансовой целостности — приоритет №1 по §96.
**Последствия**: ШАГ 15 (Deployment) требует от пользователя внешней инфраструктуры;
one-click деплоя не будет. Зафиксировано до начала разработки.

---

## ADR-007 — Реверсивная модель вместо DELETE для финансовых данных

**Статус**: ACCEPTED
**Решение**: финансовые таблицы (`operations`, `transfers`, `payroll_payments`,
`kaspi_payouts`) защищены триггером, запрещающим физический `DELETE`. Отмена — через
обратную операцию (`related_operation_id`) и статус (§16).
**Обоснование**: аудируемость, восстановимость истории, соответствие требованию
Data Integrity как приоритета №1 (§96).

---

## ADR-008 — Freeze-значения (ставки и цены) вместо ссылок на текущие

**Статус**: ACCEPTED
**Решение**: `work_entries.rate_at_time` и `order_materials.price_at_time` копируют значение
на момент создания записи (§32, §38).
**Обоснование**: изменение справочника ставок/цен не должно ретроспективно менять уже
рассчитанную зарплату и себестоимость. Реализовано триггером БД, а не только кодом
приложения — гарантия даже при прямой вставке.

---

## ADR-009 — AES-256-GCM для шифрования Kaspi-токенов

**Статус**: ACCEPTED
**Решение**: токены шифруются AES-256-GCM (не Fernet, и Fernet не называется AES-256) (§19).
**Детали**:
- Algorithm: AES-256-GCM (AEAD).
- Key storage: переменная окружения `ENCRYPTION_KEY` (32 байта, base64). **Никогда в БД** (§19).
- Nonce/IV: 12 байт, случайный на каждое шифрование, хранится в префиксе шифротекста.
- Auth tag: 16 байт, хранится в суффиксе шифротекста.
- Формат BYTEA: `key_version(1) || nonce(12) || ciphertext || tag(16)`.
- Rotation: поле `key_version` в `kaspi_api_credentials`; ротация — перешифрование скриптом
  при наличии старого и нового ключа.
**Реализация**: ШАГ 3 / DAY 3. На ШАГЕ 1 создаётся только структура хранения.

---

## ADR-010 — Идентичность операции для идемпотентности

**Статус**: ACCEPTED
**Решение**: идемпотентность обеспечивается на уровне БД: `UNIQUE (ip_id, type_id, external_id)`
в `operations` (partial index, где `external_id IS NOT NULL`) плюс fingerprint для
Excel-импорта.
**Обоснование**: retry HTTP-клиента и повторный импорт не должны создавать вторую финансовую
операцию (§41, §43, §57). Проверка «нет ли уже такой записи» в коде приложения не защищает от
гонки — нужен констрейнт БД.

---

## ADR-011 — `database/schema.sql` — единственный источник истины; autogenerate отключён

**Статус**: ACCEPTED
**Контекст**: SQLAlchemy-модели и Alembic могли бы стать источником схемы
(`alembic revision --autogenerate`), но схема STEP 1 содержит объекты, которые ORM-метаданные
не выражают: 59 триггеров и 11 функций, 3 EXCLUDE-констрейнта, 4 GENERATED-колонки,
partial UNIQUE-индексы, 5 views.
**Решение**:
1. `database/schema.sql` остаётся источником истины для физической схемы.
2. Baseline-миграция `0001_baseline_schema` исполняет этот файл дословно, а не переописывает
   DDL на Python.
3. `--autogenerate` для проекта ЗАПРЕЩЁН; в `alembic/env.py` `compare_type=False` и
   `compare_server_default=False`.
4. Соответствие моделей применённой БД проверяется автотестом
   `tests/test_models_match_schema.py` (81 проверка), а не доверием к ORM.
**Обоснование**: autogenerate против такой схемы генерирует деструктивный diff — молча удалил бы
триггеры, EXCLUDE, generated-колонки и views, то есть именно то умолчание, которое запрещает §1.
Дословное исполнение файла гарантирует, что БД, созданная Alembic, идентична БД, созданной
`scripts/apply_schema.sh` — подтверждено: audit_step1.py даёт 623/623 на обеих.
**Последствия**: последующие изменения схемы пишутся вручную двумя согласованными правками —
новая ревизия Alembic + обновление `schema.sql`; автотест соответствия ловит расхождение.

## ADR-012 — Разделение ролей БД: `erp_migrator` (DDL) / `erp_app` (DML-only)

**Статус**: ACCEPTED (TD-008 закрыт)
**Контекст**: во всех окружениях приложение подключалось ролью, которая владела схемой и могла
выполнять DDL. Нарушение наименьших привилегий (§15): компрометация приложения давала бы
DROP/ALTER над бухгалтерским ledger'ом.
**Решение**:
1. `erp_migrator` — владелец схемы, таблиц, последовательностей, views и функций; единственная
   роль, под которой выполняются миграции (`alembic upgrade head`) и `apply_schema.sh`.
2. `erp_app` — только `SELECT/INSERT/UPDATE/DELETE` на таблицы, `USAGE/SELECT` на
   последовательности, `EXECUTE` на функции; `CREATE ON SCHEMA public` отозван.
3. `scripts/split_db_roles.sh` — идемпотентный перенос владения и выдача прав; падает, если
   erp_app всё ещё владеет объектами (evidence-check).
4. CI: схема применяется как `erp_migrator`, затем split, затем весь прогон
   (self-audit → alembic baseline → alembic check → pytest) — под `erp_app`.
5. Docker: bootstrap-суперпользователь контейнера — `erp_migrator`; `erp_app` создаёт
   `docker/initdb/03-create-app-role.sh`; CMD запускает alembic как `erp_migrator`,
   uvicorn — как `erp_app`.
**Обоснование/доказательство (2026-09-15)**: на чистой БД `erp_mig_test` —
`alembic upgrade head` под `erp_migrator` (включая `CREATE EXTENSION btree_gist`), split,
негативные проверки (erp_app: `CREATE TABLE` → permission denied, `DROP TABLE users` → must be
owner), полный pytest **279/279** под `erp_app`, `alembic check` PASS, self-audit **624/624**.
Split применён к erp_test, erp_dev и erp_mig_test.
**Последствия**: миграции не могут выполняться ролью приложения (деплой обязан иметь
пароль мигратора); тесты не требуют TRUNCATE/DDL — conftest использует DELETE; TD-003
(escape-hatch `app.allow_financial_delete`) остаётся отдельным открытым пунктом.

## ADR-013 — Ревокация JWT: per-user `token_version` вместо per-token denylist (2026-09-17, v8 Phase A)

**Контекст**: §17 требует token revocation и session invalidation. JWT stateless —
отзыв отдельного токена требует server-side состояния (denylist таблица + запись
на каждый запрос) или встроенного в токен идентификатора версии.

**Решение**: `users.token_version INTEGER NOT NULL DEFAULT 1` (миграция 0003).
Каждый JWT при выпуске несёт `ver` = текущей версии пользователя;
`current_principal()` сверяет `ver` токена с БД на каждый запрос и отклоняет
несовпадение (401 token revoked). Два источника bump:
- `POST /auth/logout` — самообслуживание; семантика «logout everywhere»
  (все устройства, все токены до этого мгновения);
- `POST /auth/users/{id}/revoke-sessions` (ADMIN) — экстренная ревокация
  скомпрометированного аккаунта; audit-строка с явным действием
  `REVOKE_SESSIONS` (chk_audit_log_action расширен).
Смена/сброс пароля дополнительно bump-ают версию (детерминированная ревокация,
независимая от часов). Logout-only-на-клиенте отсутствует по построению.

**Отвергнутая альтернатива**: per-token jti denylist — таблица + write на каждый
запрос ради отзыва одного токена; для single-process внутреннего ERP избыточно.
Per-token индивидуальный отзыв остаётся недоступным (осознанно); граница экспозиции
ограничена TTL 8h и возможностью экстренной ревокации всех сессий аккаунта.

**Доказательство (2026-09-17)**: миграции 0002+0003 на чистой БД —
`alembic upgrade head` (2 шага) + `alembic check` PASS; downgrade 0003→0002→
upgrade PASS; `tests/test_token_revocation.py` 9/9 (revoked-rejected,
non-revoked-accepted, logout, concurrent sessions, admin revoke (target-only,
audit REVOKE_SESSIONS), expired, invalid token); полный pytest 297/297,
ruff 0, mypy 0.

## ADR-014 — TD-015: rate limiter на PostgreSQL, не Redis (2026-09-18)

**Контекст:** in-process limiter (§55) не разделяет состояние между workers/replicas.
**Варианты:** (a) Redis sliding window; (b) таблица в существующем Postgres.
**Решение:** (b) — таблица `auth_rate_events(bucket_key, occurred_at)`.
**Почему:** проект не имеет Redis в deployment topology (docker-compose: app+Postgres);
добавление Redis ради одного limiter'а — новая зависимость, новая точка отказа, новые
секреты. Atomicity: INSERT+COUNT в одной транзакции.
**Отказ от per-token denylist-подхода:** см. ADR-013 (та же логика уровней shared state).
**Отказ от Redis:** см. выше — Postgres уже обязателен для login, Redis добавил бы
вторую обязательную инфраструктуру ради одной функции.
**Failure policy — деградация до per-worker окна (fail-open на shared store), НЕ fail-closed:**
если `auth_rate_events` недоступна или медленнее 0.5 c (3 retry), limiter продолжает
считать попытки в in-process окне того же размера (5/60 c). Обоснование: strict fail-closed
превратил бы деградацию БД в полный отказ login даже там, где per-worker окно всё ещё
ограничивает перебор; атакующий при этом теряет главный выигрыш (умножение бюджета на
число workers) только частично, а доступность login сохраняется. Недоступность shared
store логируется (`rate_limit.shared.unavailable`) для мониторинга. Включение shared
режима — env `RATE_LIMIT_SHARED=1` (production compose; dev/CI по умолчанию off).
**Ограничение:** full multi-replica кластер не доказан (нет инфраструктуры) — BLOCKED-пункт,
механика shared-state доказана multi-process тестом на реальном Postgres.

**Уточнение реализации (2026-09-19):** политика отказа — fail-open-to-local:
при недоступности/таймауте shared-store (3 попытки, бюджет 0.5 с) limiter не
блокирует запрос, а продолжает считать попытки в per-worker in-process окне
(5/60с), поэтому DB-сбой не выводит login из строя. События, пропущенные
shared-стором за время сбоя, не восстанавливаются (acceptable: окно 60 с).
Shared-режим включается только при `RATE_LIMIT_SHARED=1` (Docker/compose
production; dev без него). Доказательство: test_fail_open_when_shared_unreachable.
**Hardening (2026-09-19, тот же PR):** advisory lock (`pg_advisory_xact_lock`)
внутри shared-пути выполняется с `SET LOCAL lock_timeout=600ms` /
`statement_timeout=700ms` — контеншн или зависший bucket не могут удержать
login дольше заявленного fail-open бюджета (дефолтный app-wide
statement_timeout 30 с превышал бы 0.5-секундный бюджет). Доказательство:
test_shared_lock_contention_fails_open (удержанный lock → деградация <5 с).

## ADR-015 — Cross-IP transfer: парные операции и идемпотентность (2026-09-24)

**Статус**: ACCEPTED для cross-IP transfers; реализация проверена 2026-09-26 (evidence — `docs/PROGRESS.md`).

**Контекст**: REQ-0109/REQ-0204 требуют разные ИП и `TRANSFER_OUT` + `TRANSFER_IN`, не влияющие на P&L. FINANCE-OPS-001 §24 дополнительно требует source/destination accounts. OQ-009 касается банковских реквизитов, выписок и интеграции; он не отменяет использование существующих generic `accounts`.

**Решение**:
- Поддерживается только cross-IP transfer (`from_ip_id <> to_ip_id`). Перевод между счетами одного ИП не реализуется без отдельного business decision.
- Запрос обязан назвать активный source account и destination account; каждый account должен принадлежать соответствующему IP. Это фиксирует внутреннее движение между generic accounts, но не подтверждает банковский остаток или выписку.
- Один запрос в одной DB-транзакции создаёт `Transfer`, posted `TRANSFER_OUT` с отрицательной суммой на source IP/account и posted `TRANSFER_IN` с равной положительной суммой на destination IP/account. Ссылки `Transfer.outbound_operation_id`/`inbound_operation_id` и взаимные `Operation.related_operation_id` сохраняют связь.
- Для POST обязателен `Idempotency-Key`, уникальный в `transfers`: тот же ключ с тем же payload возвращает прежний transfer и те же leg IDs; тот же ключ с другим payload отклоняется HTTP 409. Уникальный DB index защищает от конкурентного дубля; TTL нет.
- Endpoint требует ACCOUNTANT+ и проверяет доступ к обоим IP до поиска результата повторного ключа. Поскольку существующая policy даёт весь IP-scope только ADMIN/DIRECTOR, ограниченный одному IP пользователь не может выполнить cross-IP transfer.
- `TRANSFER_OUT`/`TRANSFER_IN` остаются вне income, expense и P&L. Database composite FKs закрепляют принадлежность account к IP. Валидированный `chk_transfers_posted_has_legs` запрещает POSTED aggregate без обеих legs; deferred constraint triggers дополнительно проверяют направление, IP/account, сумму/знак, тип, дату, actor, reason, взаимные `related_operation_id` и единственную привязку каждой leg к одному transfer.
- Миграция проверяет существующий ledger до установки triggers и валидирует constraint/FKs. Если POSTED transfer или transfer operation не удовлетворяет инвариантам, миграция завершается ошибкой и не создаёт synthetic legs; требуется явная reconciliation до deployment.
- Сервис и database trigger запрещают одиночное reversal transfer leg; парный transfer reversal/correction остаётся отдельным решением и не подменяется изменением одной ноги.

**Последствия**: исторические корректно связанные transfer-строки сохраняются; непарные или несогласованные POSTED строки блокируют upgrade до ручной reconciliation. Новые запросы проходят atomic service path и database checks. OQ-009 остаётся открытым для банковских реквизитов, форматов выписок и интеграции; OQ-006/ADR-007 сохраняют общий reversal-контекст.
