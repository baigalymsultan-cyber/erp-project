# REVERSAL DECISION MEMO

**Дата:** 2026-10-01
**Статус:** BLOCKED — требуется бизнес-решение владельца
**Основание:** Master Prompt §1, §54 (Phase 0 Discovery, фокус Reversal); правило финансовой безопасности — не выдумывать бухгалтерскую семантику.
**Связанные записи:** ADR-007 (ACCEPTED), ADR-015, TD-018 (OPEN), TD-019, OQ-006.

---

## 1. ВОПРОС

Какая модель отмены (reversal / сторно) проведённой финансовой операции (`POSTED`) является утверждённой для проекта:

- (A) **компенсирующая операция** — исходная строка остаётся, создаётся отдельная обратная операция противоположного экономического эффекта с явной связью;
- (B) **status flip** — исходная строка переходит `POSTED → REVERSED`, исключение из итогов происходит фильтром статуса;
- (C) гибрид — исходная строка помечается `REVERSED` **и** создаётся компенсирующая операция.

Без ответа на этот вопрос нельзя реализовывать ADR-016, закрывать TD-018 и менять `reverse_operation()`.

---

## 2. ТЕКУЩЕЕ ПОВЕДЕНИЕ (факты, проверено чтением кода)

**Сервис** — `app/services/__init__.py:496`, `reverse_operation(session, operation_id, comment=None)`:
1. Загружает `Operation` по id; несуществующая → `ValueError`.
2. Проверяет, не является ли операция leg'ом transfer (`transfers.outbound_operation_id / inbound_operation_id`); leg → `ValueError("a transfer leg cannot be reversed independently")`.
3. Требует `status == "POSTED"`, иначе `ValueError`.
4. Меняет **ту же строку**: `op.status = "REVERSED"`; при переданном `comment` перезаписывает `op.comment`.
5. `session.flush()`; возвращает `{"id", "status"}`.
6. Компенсирующая операция **не создаётся**, `related_operation_id` **не заполняется**.

**API** — `app/api/__init__.py:269`, `POST /operations/{operation_id}/reverse`:
- требует привилегию `ACCOUNTANT`, затем `ensure_ip_access(session, principal, target.ip_id)`, затем вызывает сервис и делает `session.commit()`.
- Ответ: `{"id", "status": "REVERSED"}`; 404 если операция не найдена.

**P&L / отчётность:**
- `pnl()` (`app/services/__init__.py:574`) учитывает только `status='POSTED'`; docstring прямо фиксирует: сервис зеркалит фильтр view.
- `v_ip_financial_summary` (`database/schema.sql:2026`): `JOIN operations o ON ... AND o.status = 'POSTED'`; комментарий: "reversed operations excluded (§16)".
- Итог: reversal сегодня работает **исключением из итогов**, а не компенсацией.

**Защита БД (FIN-OPS-0001, коммит 922da53, верифицирован 2026-09-30):**
- `fn_protect_posted_ledger_operation()` + `trg_operations_protect_posted_ledger` запрещают изменение финансовых полей POSTED/REVERSED не-transfer операций; переход `POSTED → REVERSED` явно разрешён как совместимость до ADR-016.
- `tests/test_ledger_immutability.py` фиксирует это поведение как временный контракт; `test_api.py:227` проверяет status flip через API.

---

## 3. ОБНАРУЖЁННЫЙ КОНФЛИКТ

| Источник | Требование |
|---|---|
| ADR-007 (ACCEPTED) | "Отмена — через обратную операцию (`related_operation_id`) и статус (§16)" |
| Master Prompt §1 | Исходный финансовый факт остаётся; создаётся отдельная компенсирующая операция противоположного экономического эффекта; явная связь |
| Текущий код | Status flip без компенсирующей операции и без связи |

Дополнительные факты, усиливающие конфликт:

1. **`related_operation_id` уже занят.** Используется для двусторонней связи transfer legs (`app/services/__init__.py:967-968`: `outbound.related_operation_id = inbound.id` и наоборот). Комментарий колонки (`database/schema.sql:1458`): "Link to the reversed/corrected operation, and pairs TRANSFER_OUT with TRANSFER_IN (§16, §28)" — одна колонка на два разных смысла.
2. **TD-018 прямо запрещает выводить семантику из этой колонки:** "Нет подтверждённой бизнес-семантики для компенсирующего парного ledger-события; не выводить её из generic related_operation_id".
3. **CHECK constraint блокирует наивную компенсацию.** `chk_operation_types_dir_kind` (`database/schema.sql:759`): `INCOME→IN`, `EXPENSE→OUT`, `TRANSFER→IN/OUT`. Компенсация продажи `SALE_KASPI_SHOP` (IN/INCOME) требует движения OUT, но `kind='INCOME'` с `direction='OUT'` нарушит CHECK. Варианты — новый `kind` (миграция + P&L-семантика), переиспользование `EXPENSE` (бухгалтерски сомнительно: возврат ≠ расход), отдельный тип `REVERSAL` (нет в seed, нет семантики `affects_pnl`). Это **бухгалтерское решение**, а не техническое.
4. **OQ-006** (правила возвратов Kaspi: полный/частичный, влияние на payout) открыт — reversal возвратов упирается в него.
5. **TD-019:** `reverse_operation()` перезаписывает `comment` у POSTED-строки — остаточная изменяемость provenance до ADR-016.

---

## 4. DECISION

**Решение не принято.** Статус: **BLOCKED**.

Причина: выбор между (A), (B) и (C) определяет бухгалтерскую семантику (что считается доходом/расходом/возвратом, как пара отображается в P&L и cash), а существующие требования дают противоречивые указания (ADR-007 vs фактический код + тесты + TD-018). Самостоятельный выбор нарушил бы правило финансовой безопасности.

---

## 5. ALTERNATIVES CONSIDERED

### Вариант A — Компенсирующая операция (ADR-007 / Master Prompt)

Исходная операция остаётся `POSTED` (или переходит в `REVERSED` — подвопрос A1). Создаётся новая операция противоположного экономического эффекта, связанная через `related_operation_id` (или новую связь — подвопрос A2).

Технические последствия:
- Требуется решение по типу компенсирующей записи: новый `kind`/`code` (миграция seed + изменение `chk_operation_types_dir_kind` или новый тип), либо маппинг на существующие типы (бухгалтерское решение).
- Требуется решение по `affects_pnl` компенсирующей записи и по её отображению в `v_ip_financial_summary` (сейчас view считает только `POSTED`; пара POSTED+компенсация даст нетто-ноль, но валовые обороты вырастут — это влияет на отчётность).
- Требуется idempotency-ключ reversal (повторный вызов не должен создавать вторую компенсацию).
- Требуется отдельное поле `reason` (сейчас reason пишется в `comment`, перезаписывая исходный — TD-019).
- Требуется разделить смысл `related_operation_id` (transfer-pair vs reversal-link) или ввести отдельную колонку.
- Миграция данных не требуется (исторических REVERSED-строк с компенсацией нет — их не существует в природе текущей модели).

### Вариант B — Status flip (текущее поведение, узаконить)

Оставить `reverse_operation()` как есть: `POSTED → REVERSED` на той же строке, исключение из P&L фильтром статуса.

Технические последствия:
- Минимальные изменения кода (фактически ноль).
- Требует пересмотра ADR-007 (статус ACCEPTED → SUPERSEDED/AMENDED) — решение владельца.
- Ограничения: нет явной аудит-связи "что компенсирует что" (только статус + перезаписанный comment); невозможен частичный reversal (только вся сумма); повторный reversal запрещён кодом (REVERSED → REVERSED отклоняется).
- P&L уже корректен для этой модели.

### Вариант C — Гибрид

Исходная строка → `REVERSED` **плюс** компенсирующая `POSTED`-операция.

Технические последствия:
- Двойной учёт эффекта, если P&L одновременно исключает REVERSED и включает компенсацию — нетто может оказаться корректным, но модель избыточна и нигде не задокументирована.
- Все технические затраты варианта A плюс риск двойного списания при ошибке фильтров.
- Не рекомендован без явного бизнес-требования.

---

## 6. FINANCIAL SEMANTICS (открытые подвопросы)

- F1. Является ли возврат продажи (`SALE_KASPI_SHOP`/`SALE_KASPI_PAY`) расходом, отрицательным доходом или отдельной категорией? (Влияет на `kind` компенсирующей записи.)
- F2. Возможен ли частичный reversal (часть суммы)? Текущий код — только полная сумма статусом.
- F3. Должен ли reversal автоматически тянуть связанные сущности (payout items, payroll, commission)? OQ-006 открыт.
- F4. Что происходит с `comment`/provenance исходной строки при reversal? (TD-019: сейчас comment перезаписывается.)

---

## 7. P&L / CASH / IDEMPOTENCY / AUDIT / AUTHORIZATION

- **P&L:** при варианте A view и `pnl()` должны учитывать компенсирующую запись; валовые обороты вырастут, нетто останется. Требуется решение: показывать ли сторно свёрнуто или развёрнуто в `v_ip_financial_summary`.
- **Cash:** reversal не двигает деньги сам по себе (нет движения по `accounts`); если возврат денег — отдельная операция или часть reversal? Не определено.
- **Idempotency:** повторный `POST /operations/{id}/reverse` сейчас отклоняется (статус уже REVERSED). При варианте A нужен идемпотентный ключ, чтобы повтор не создавал вторую компенсацию.
- **Audit:** при варианте A связь должна быть двусторонне проверяемой (исходная ↔ компенсация); `audit_log` должен фиксировать оба события с `actor`.
- **Authorization:** сейчас `ACCOUNTANT` + IP scope. Должна ли отмена требовать отдельного права (например, `ACCOUNTANT_REVERSAL`) или второго подтверждающего? Не определено.

---

## 8. ЗАТРОНУТЫЕ ФАЙЛЫ (при любом варианте, кроме B)

- `app/services/__init__.py` — `reverse_operation()`, `pnl()`
- `app/api/__init__.py` — `POST /operations/{operation_id}/reverse`
- `app/models/finance.py` — `Operation`, `related_operation_id`
- `database/schema.sql` — `operations`, триггеры `trg_operations_protect_posted_ledger` / `trg_forbid_delete`, `operation_types` seed, `chk_operation_types_dir_kind`, `v_ip_financial_summary`
- `alembic/versions/` — новая миграция (0008)
- `tests/test_api.py`, `tests/test_ledger_immutability.py` — обновление контракта
- `docs/ADR.md` — ADR-016; `docs/TECHNICAL_DEBT.md` — закрытие TD-018/TD-019

---

## 9. НЕОБХОДИМОЕ РЕШЕНИЕ (вопросы владельцу)

- **Q1.** Выбранная модель: (A) компенсирующая операция, (B) узаконить status flip, (C) гибрид?
- **Q2.** Если (A): каким типом отражается компенсация (новый kind/code vs переиспользование)? Как она влияет на P&L (`affects_pnl`)?
- **Q3.** Если (A): остаётся ли исходная операция `POSTED` или переходит в `REVERSED`? Чем связывается пара (`related_operation_id` vs новая колонка)?
- **Q4.** Допустим ли частичный reversal (часть суммы)?
- **Q5.** Требуется ли отдельное поле `reason` вместо перезаписи `comment`?
- **Q6.** Требуется ли отдельное право/подтверждение для reversal (сверх `ACCOUNTANT`)?
- **Q7.** Как пара reversal отображается в `v_ip_financial_summary` (свёрнуто/развёрнуто)?

---

## 10. RESEARCH (2026-10-03) — production reversal patterns

Глубокое исследование 7 production accounting systems (Oracle Financials,
SAP, Microsoft D365 Finance/BC, Odoo, NetSuite, QuickBooks Online, Xero).
Полный отчёт: `docs/REVERSAL_RESEARCH_2026-10-03.md` (детальные находки:
`docs/research/reversal_oracle.md`, `docs/research/reversal_sap.md`,
`docs/research/reversal_d365_odoo_others.md`).

**TL;DR:**
1. **Ни одна из 7 систем не делает reversal через status flip posted-строки.** Консенсус: compensating journal — новая сторнирующая проводка, оригинал неизменен. Status flip — только аннотация (Canceled/Voided/Reversed), не финансовый механизм.
2. Оригинал получает двустороннюю ссылку на реверс (SAP STBLG, Oracle Reversal Status, Odoo reversed_entry_id).
3. Reversal-of-reversal запрещён везде, кроме D365 Finance.
4. Дата реверса никогда не попадает в закрытый период; posting-lock — жёсткий гейт.
5. Correction / reversal / void / refund — 4 разные операции везде.
6. Исторический P&L защищается закрытием периодов, не флагом.
7. Идемпотентность state-based («reverse only once»); API idempotency-key (C-1 в erp-kaspi) строже индустрии.

**Маппинг на Q1–Q7 (§9):**

| Вопрос | Что говорит исследование | Кто решает |
|---|---|---|
| Q1. Модель A/B/C | Консенсус: A или C. Чистый B не используется никем как финансовый механизм | **OWNER** — с референсом: B противоречит практике |
| Q2. Тип компенсации, affects_pnl | Индустрия решает reversal reason'ами (SAP STGRD, Odoo Storno); прямого ответа «каким kind» для малого бизнеса нет | **OWNER** (бухгалтерское решение) |
| Q3. Оригинал POSTED vs REVERSED; связь пары | Консенсус: оригинал остаётся posted + маркер + двусторонняя ссылка; `related_operation_id` переиспользовать нельзя (TD-018) | Система (техника), подход подтверждает owner |
| Q4. Частичный reversal | Естественно ложится на compensating-модель (Odoo draft credit note); status flip частичности не даёт | **OWNER** (нужен ли), техника — система |
| Q5. Отдельное поле reason | Консенсус: да; перезапись comment оригинала — антипаттерн | **Система** |
| Q6. Право/подтверждение | Oracle: journal approval rules; erp-kaspi уже имеет ACCOUNTANT + IP scope | **OWNER** (достаточно ли ACCOUNTANT) |
| Q7. Пара в v_ip_financial_summary | SAP negative posting существует чтобы не раздувать обороты — вопрос реальный | **OWNER** (свёрнуто vs развёрнуто), техника — система |

**Система может решить сама:** двусторонние ссылки + маркеры, поле reason, state-guard + idempotency-key, таксономия correction/reversal/void/refund, минимальный posting-lock, запрет реверса реверса.

**Остаётся за OWNER:** модель A vs C, бухгалтерский тип компенсации и P&L-семантика, частичный reversal, уровень posting-lock, свёрнуто/развёрнуто в отчётности, достаточно ли ACCOUNTANT.

---

## 11. STATUS

**BLOCKED — BUSINESS DECISION REQUIRED.**

До получения ответов на Q1–Q7:
- `reverse_operation()` не изменяется;
- ADR-016 не пишется;
- TD-018 остаётся OPEN;
- тесты `test_ledger_immutability.py` / `test_api.py` продолжают фиксировать текущее поведение как временный контракт.

Независимые работы (не затрагивающие reversal semantics) могут продолжаться.
