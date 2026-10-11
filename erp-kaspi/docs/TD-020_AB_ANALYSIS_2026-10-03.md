# TD-020 — Variant A vs Variant B: full technical analysis

**Дата:** 2026-10-03
**Статус:** research only. Код не меняется. OWNER принимает бизнес-решение.
**База:** `docs/TD-020_PAYROLL_STATUS_DECISION.md` (5 production-систем), код прочитан 2026-10-03.
**Формат:** для каждого варианта — цепочка
`RULE → DB MODEL → STATUS TRANSITIONS → PARTIAL PAYMENT → FAILED PAYMENT → REPEAT PAYMENT → REPORTING → AUDIT`.

---

## 0. Общие факты (оба варианта)

| Элемент | Факт (проверен чтением кода) |
|---|---|
| Текущий баг | `pay_payroll_summary()` (`app/services/__init__.py:475-477`) ставит `summary.status="PAID"` **и** `period.status="PAID"` + `period.closed_at` после оплаты **одного** summary. Период с N сотрудниками блокируется для сотрудников 2..N (`calculate_payroll()` отказывает `PAID`-периодам, стр. 258-261). Зафиксировано тестом `tests/test_payroll_flow.py:191` как known-bug evidence. |
| Деньги | `payroll_payments`: `status CHECK IN ('POSTED','REVERSED')`, `uq_payroll_payments_active` — частичный уникальный индекс «максимум один POSTED платёж на summary» (§57, race-safe). **Ни одна функция в коде не выставляет `REVERSED`** — значение в CHECK есть, записи нет. Failed/void-платёж сегодня не смоделирован. |
| Summary lifecycle | `CALCULATED → APPROVED → PAID` (`calculate_payroll` → `approve_payroll_summary` → `pay_payroll_summary`). `CANCELLED` — мёртвое значение (TD-021), кодом не выставляется. |
| Period guard | `calculate_payroll()` требует `period.status NOT IN ('CLOSED','PAID')`; ставит `OPEN → PROCESSING`. |
| Audit | `trg_payroll_periods_audit` (A-1, migration 0009) — `fn_audit()` пишет `audit_log(user_id=fn_current_app_user(), entity, entity_id, action, old_data, new_data)`. Переходы статуса периода уже в аудите с актором. |
| View | `v_payroll_due` (§63) показывает `period_status` + `amount_due` построчно на summary; **фильтра по `period.status` нет**. Frontend читает только `/reports/payroll-due/summary` (KPI, `frontend/app.py:1111`) — зависимости от `period.status='PAID'` в UI/отчётах **нет** (grep 2026-10-03). |
| Нулевые суммы | `calculate_payroll()` не создаёт summary с нулевым итогом («zero summary is ledger noise»). |

**Ключевая семантическая трещина (оба варианта):** признак «оплачено» сегодня — это `summary.status='PAID'`, а не наличие POSTED-платежа. После введения void-платежа (статус `REVERSED` уже в CHECK) возможна комбинация `summary=PAID + payment=REVERSED`. Любая проверка «все оплачены» должна решать, смотрит ли она на `summary.status` или на наличие POSTED `payroll_payments`. Ниже это отражено.

---

## 1. Вариант A — производный статус периода

### RULE

`pay_payroll_summary()` выставляет `PAID` **только** на `payroll_summary`. `period.status="PAID"` выставляется внутри той же транзакции **только если** все *платёжеспособные* summaries периода в `PAID`.

Точная проверка (SQLAlchemy, внутри транзакции после `session.flush()`):

```python
remaining = session.scalar(
    select(func.count())
    .select_from(PayrollSummary)
    .where(
        PayrollSummary.payroll_period_id == period.id,
        PayrollSummary.status.in_(("CALCULATED", "APPROVED")),  # ещё не оплачены
    )
)
if remaining == 0:
    period.status = "PAID"
    period.closed_at = datetime.now(UTC)
```

Почему именно `IN ('CALCULATED','APPROVED')`, а не `<> 'PAID'`:
- `CANCELLED` — мёртвое значение (код не выставляет), но если когда-нибудь появится, `<> 'PAID'` заблокирует период навсегда. Явное перечисление «неоплаченных» состояний безопаснее.
- Нулевые summaries не создаются (`calculate_payroll` их пропускает) — знаменатель «все summaries периода» корректен без доп. фильтра по сумме.

### DB MODEL

**Изменений схемы нет.** CHECK `('OPEN','PROCESSING','CLOSED','PAID')` остаётся. Новое поведение полностью в коде `pay_payroll_summary()`.

**Но требуется новая блокировка** (см. CONCURRENCY ниже): `SELECT ... FOR UPDATE` строки `payroll_periods` в `pay_payroll_summary()` — по образцу §57-лока в `calculate_payroll()` (стр. 267). Без неё вариант A некорректен в race.

### STATUS TRANSITIONS

```
OPEN → PROCESSING   (calculate_payroll, без изменений)
PROCESSING → PAID   (pay_payroll_summary, только когда последний неоплаченный summary оплачен)
PAID — терминальный (calculate_payroll отказывает, как сейчас)
```

Переход `PROCESSING → PAID` — **автоматический**, без явного действия пользователя. Возврат `PAID → PROCESSING` не определён (см. FAILED PAYMENT).

### PARTIAL PAYMENT

Штатный сценарий: оплачен 1 из N → `summary=PAID`, `period` остаётся `PROCESSING`. `calculate_payroll()` для остальных сотрудников работает (guard `NOT IN ('CLOSED','PAID')` теперь корректен — PAID означает «все оплачены»). Досчёт/доплата отдельных сотрудников возможны в любой момент.

### CONCURRENCY (ответ на прямой вопрос)

Проблема: сегодня `pay_payroll_summary()` лочит **только строку summary** (`with_for_update()`, стр. 419). Сценарий «два бухгалтера одновременно платят двух последних сотрудников»:

1. Txn A лочит summary-1, проверяет «все PAID?» → видит summary-2 ещё `APPROVED` (незакоммичен) → **не** ставит `PAID` на период. Коммитит.
2. Txn B лочит summary-2, проверяет «все PAID?» → видит summary-1 уже `PAID` (A закоммитил) → ставит `period=PAID`. Коммитит.

В этом порядке — повезло. Обратный порядок (B проверяет до коммита A): **обе** транзакции видят «есть неоплаченные» → **ни одна** не ставит `PAID` → все summaries `PAID`, период навсегда `PROCESSING`. Тихий deadlock семантики: деньги розданы, период висит.

**Решение (обязательно для A):** в начале `pay_payroll_summary()` добавить

```python
session.execute(
    select(PayrollPeriod.id)
    .where(PayrollPeriod.id == summary.payroll_period_id)
    .with_for_update()
)
```

Тогда платежи по одному периоду сериализуются: вторая транзакция ждёт первую, видит её коммит и корректно доводит период до `PAID`. Побочный эффект — сериализация всех выплат периода (приемлемо: выплаты — редкие ручные операции бухгалтера).

Альтернатива без лока — периодический reconciler, дотягивающий `PROCESSING`-периоды со всеми `PAID`-summaries. Это фоновый процесс, новая сущность, сложнее. Лок проще и соответствует §57-паттерну кодовой базы.

### FAILED PAYMENT

Сегодня: void-платежа нет (`REVERSED` в CHECK, код не пишет). Если/когда void появится:

- Под A период уже `PAID` (все были оплачены), деньги вернулись. Период **не откатывается автоматически** — нет правила `PAID → PROCESSING`.
- Evidence: Oracle `Void a Payment` → автовыпуск в следующем прогоне, период не трогается; SAP — off-cycle корректировка после Exit; D365 — reversing pay statement отдельным документом. **Ни одна система не откатывает статус периода из-за возврата выплаты.**
- Значит для A нужно правило (OWNER, вопрос 4 исходного memo): (a) оставить `PAID` + корректирующая выплата в новом периоде (SAP-паттерн); (b) разрешить ручной откат `PAID → PROCESSING`. Система сама это не выведет — бизнес-семантика.
- Дополнительная трещина: если void обнуляет платёж, а `summary.status` остаётся `PAID`, проверка «все PAID» врёт. При введении void придётся либо сбрасывать `summary.status → APPROVED`, либо проверять наличие POSTED-платежа (`EXISTS (payroll_payments WHERE status='POSTED')`), а не статус summary. Это касается обоих вариантов.

### REPEAT PAYMENT

Без изменений: `summary.status='PAID'` → `ValueError` (§57); `uq_payroll_payments_active` блокирует второй POSTED-платёж даже в race. Под A повторная оплата последнего сотрудника невозможна — инвариант «PAID-период = деньги розданы» сохраняется.

### REPORTING

- `v_payroll_due`: частично оплаченный период → `period_status='PROCESSING'`, строки: `amount_due=0` у оплаченных, `>0` у остальных. После полной оплаты → `period_status='PAID'`, все `amount_due=0`.
- Потребителей `period.status='PAID'` в UI/отчётах нет (проверено grep) — отчётность не ломается.
- Возвращаемый payload `post_payroll_pay` содержит `period_status` — значение меняется с «PAID после первой выплаты» на «PROCESSING/PAID по факту». Форма ответа та же.

### AUDIT

`trg_payroll_periods_audit` фиксирует `PROCESSING → PAID` один раз (вместо `PROCESSING → PAID` после первой выплаты сегодня — семантически ложная запись). `old_data/new_data` + `fn_current_app_user()` дают кто/когда. Аудит становится **честнее**, строк меньше.

### Что нужно изменить (A)

1. `app/services/__init__.py::pay_payroll_summary`: убрать безусловные `period.status="PAID"` / `closed_at`; добавить `FOR UPDATE` на период + условный переход при `remaining == 0`.
2. `tests/test_payroll_flow.py:191` — переписать: после оплаты 1 из 2 период `PROCESSING`, не `PAID` (тест кодирует баг — переписывание допустимо).
3. Новых миграций/эндпоинтов/UI — нет.

---

## 2. Вариант B — явное закрытие, убрать PAID у периода

### RULE

`payroll_periods.status` — чисто процесс: `OPEN → PROCESSING → CLOSED`. Статус `PAID` у периода **убирается из CHECK**. Деньги живут только в `payroll_summaries.status` + `payroll_payments`.

Новая операция — явное закрытие периода ответственным (аналог SAP `Exit Payroll`, Odoo `Mark as Paid` на пакете):

```python
def close_payroll_period(session: Session, period_id: int, closed_by: int | None = None) -> dict[str, object]:
    """OPEN/PROCESSING → CLOSED. Только если все платёжеспособные summaries PAID.

    1. SELECT ... FOR UPDATE строки периода (сериализация с pay_payroll_summary);
    2. статус должен быть OPEN или PROCESSING (CLOSED → ValueError);
    3. NOT EXISTS (summaries WHERE status IN ('CALCULATED','APPROVED'))
       иначе ValueError со списком неоплаченных employee_id;
    4. period.status = 'CLOSED'; period.closed_at = now().
    """
```

Эндпоинт:

```
POST /payroll/periods/{period_id}/close   (privilege: ACCOUNTANT)
→ {"period_id":…, "status": "CLOSED", "closed_at": …}
```

`pay_payroll_summary()` **вообще не трогает** `period.status` (убираются строки 475-477 целиком).

### DB MODEL

Миграция `0011_payroll_period_close` (образец — `0009`/`0010`):

```sql
-- upgrade
ALTER TABLE payroll_periods DROP CONSTRAINT chk_payroll_periods_status;
ALTER TABLE payroll_periods ADD CONSTRAINT chk_payroll_periods_status
    CHECK (status IN ('OPEN', 'PROCESSING', 'CLOSED'));
-- data: существующих PAID-строк в проде нет (проект до продакшена),
-- но миграция обязана их обработать:
UPDATE payroll_periods SET status = 'CLOSED'
    WHERE status = 'PAID'
      AND NOT EXISTS (SELECT 1 FROM payroll_summary ps
                      WHERE ps.payroll_period_id = payroll_periods.id
                        AND ps.status IN ('CALCULATED','APPROVED'));
UPDATE payroll_periods SET status = 'PROCESSING'
    WHERE status = 'PAID';  -- остаток: была первая выплата, не все оплачены
COMMENT ON COLUMN payroll_periods.status IS 'OPEN | PROCESSING | CLOSED (§34).';
```

```sql
-- downgrade
ALTER TABLE payroll_periods DROP CONSTRAINT chk_payroll_periods_status;
ALTER TABLE payroll_periods ADD CONSTRAINT chk_payroll_periods_status
    CHECK (status IN ('OPEN', 'PROCESSING', 'CLOSED', 'PAID'));
```

Плюс правка `database/schema.sql:892` и `901` (источник истины, ADR-011), модели `app/models/hr.py` (`CheckConstraint`, комментарий `OPEN | PROCESSING | CLOSED | PAID` → без `PAID`), guard в `calculate_payroll()` (`in ("CLOSED","PAID")` → `== "CLOSED"`).

Опционально (не обязательно): колонка `closed_by BIGINT REFERENCES users(id)` — кто закрыл. `fn_audit()` уже пишет актора в `audit_log`, но запрос «кто закрыл период» по `audit_log` тяжелее, чем по колонке. OWNER решает, нужна ли.

### STATUS TRANSITIONS

```
OPEN → PROCESSING   (calculate_payroll, без изменений)
PROCESSING → CLOSED (close_payroll_period, явное действие, только если все summaries PAID)
OPEN → CLOSED       (допустимо? период без summaries — закрытие пустого периода.
                     Решение: разрешить, это осмысленно — «в этом месяце нечего платить».
                     Альтернатива — требовать PROCESSING; OWNER)
CLOSED — терминальный. Переоткрытия нет (см. ROLLBACK).
```

`CLOSED` оживает (сегодня мёртв) — заодно закрывается TD-021.

### PARTIAL PAYMENT

Период остаётся `PROCESSING` сколько угодно. `close_payroll_period()` отказывает с `ValueError("не оплачены: employee_id=[…]")` — явный список, бухгалтер видит кого доплатить. Никаких тихих состояний.

### CONCURRENCY

`close` берёт `FOR UPDATE` на строке периода. Гонка «закрытие vs оплата последнего сотрудника»:

- close первый: лочит период, видит summary `APPROVED` → отказывает. Бухгалтер доплачивает, повторяет close. Корректно.
- pay первый: лочит период (нужен тот же `FOR UPDATE` в `pay_payroll_summary()` — добавить, как и в A), коммитит `PAID`; close ждёт лок, затем видит все `PAID` → закрывает. Корректно.

Без `FOR UPDATE` в `pay_payroll_summary()` close может проскочить между проверкой и коммитом выплаты — та же сериализация нужна, что и в A. **Вывод: оба варианта требуют лока строки периода в `pay_payroll_summary()`.**

### FAILED PAYMENT

- До закрытия: void-платеж (когда появится) → summary без POSTED-платежа. Если close проверяет только `summary.status`, он пропустит «оплаченный» summary с void-платежом. **Проверка close должна учитывать платежи**, а не только статус: `NOT EXISTS (summaries s WHERE NOT EXISTS (POSTED payment for s))`. Это строже и честнее, чем в A. Требует решения по семантике void (см. §0).
- После закрытия: период `CLOSED` не трогается. Корректировка — в новом периоде (SAP off-cycle паттерн; Oracle reissue). Evidence однозначен: закрытый период не «портится» возвратом.

### REPEAT PAYMENT

Без изменений (§57, `uq_payroll_payments_active`). Закрытый период дополнительно защищён guard'ом `calculate_payroll()`.

### REPORTING

- `v_payroll_due`: фильтра по `period.status` нет — **не ломается**. `period_status` показывает `PROCESSING`/`CLOSED`.
- Потребители `period.status='PAID'` (grep 2026-10-03):
  - `app/services/__init__.py:258` — guard `calculate_payroll()` → меняется на `== "CLOSED"`;
  - `tests/test_payroll_flow.py:191` — переписать (ожидать `PROCESSING` после выплат, `CLOSED` после close);
  - frontend (`app.py`, `reports.py`), SQL views — **зависимостей нет**;
  - payload `post_payroll_pay.period_status` — форма та же, значение `PROCESSING` вместо `PAID`.
- Blast radius B: миграция CHECK + 1 guard + 1 тест + новый сервис/эндпоинт + кнопка в UI (опционально, UI периодами почти не пользуется).

### ROLLBACK (ответ на прямой вопрос)

Сценарий «закрыли преждевременно» (например, summary был miscalculated, все помечены PAID ошибочно):

- **Технически** отката нет: `CLOSED` терминальный, переоткрытия не существует.
- **По evidence** (SAP, Odoo): закрытый период не переоткрывают; корректировка — отдельным прогоном/документом в новом периоде (SAP off-cycle `PUOC_XX`, D365 reversing pay statement, Oracle void+reissue). Odoo прямо запрещает откат пакета, если есть `Paid`-листки.
- **Практическое восстановление** в erp-kaspi: сторнировать зарплатную операцию через `reverse_operation()` (REVERSED-статус уже есть, §16), создать корректирующий summary в новом периоде. Период остаётся `CLOSED` — история не переписывается (соответствует «не переписывать финансовую историю»).
- **Если OWNER хочет переоткрытие** — это новая семантика (`CLOSED → PROCESSING` с отдельным правом, аудитом и правилом «только если нет POSTED-платежей»), ни одна исследованная система её не даёт. Не рекомендуется без явного бизнес-требования.

### AUDIT

`trg_payroll_periods_audit` фиксирует `PROCESSING → CLOSED` с актором (`fn_current_app_user()`). Закрытие — осмысленное аудит-событие уровня «ответственное действие», в отличие от сегодняшнего авто-`PAID`. Если добавится `closed_by` — дублирование с `audit_log`, но удобный запрос.

### Что нужно изменить (B)

1. `database/schema.sql:892,901` + миграция `0011` (CHECK + data-migration `PAID → CLOSED/PROCESSING`).
2. `app/models/hr.py` — `CheckConstraint` + комментарий.
3. `app/services/__init__.py`:
   - `pay_payroll_summary()`: убрать строки 475-477; добавить `FOR UPDATE` на период;
   - `calculate_payroll()`: guard `in ("CLOSED","PAID")` → `== "CLOSED"`;
   - новый `close_payroll_period()`.
4. `app/api/__init__.py` — `POST /payroll/periods/{period_id}/close` (ACCOUNTANT, `ensure_ip_access` по сотрудникам периода — проверить существующий паттерн).
5. `tests/test_payroll_flow.py:191` — переписать; новые тесты: close отказывает при неоплаченных; close идемпотентен на CLOSED (ValueError); race pay-vs-close.
6. Frontend: кнопка закрытия — опционально (UI периодами почти не пользуется; API достаточно).

---

## 3. Сравнительная таблица

| Критерий | A — производный PAID | B — явное закрытие |
|---|---|---|
| Дифф | Малый: 1 функция + лок + 1 тест | Средний: миграция CHECK + data-migration + сервис + эндпоинт + guard + тесты |
| Новые сущности | Нет | `close_payroll_period()`, `POST …/close` |
| CHECK `payroll_periods.status` | Без изменений | `PAID` убирается (миграция 0011) |
| `CLOSED` / TD-021 | Остаётся мёртвым, TD-021 открыт | Оживает, TD-021 закрывается тем же решением |
| Кто ставит финальный статус | Автоматика в транзакции выплаты | Человек (ACCOUNTANT) явным действием |
| Race «последние две выплаты» | Требует `FOR UPDATE` на периоде, иначе тихий висяк `PROCESSING` | Тот же лок нужен; close-vs-pay сериализуется тем же локом |
| Частичная оплата | `PROCESSING`, всё работает | `PROCESSING`, close отказывает со списком неоплаченных |
| Преждевременное завершение | Невозможно по построению (только когда все PAID) | Возможно (человеческий фактор) → восстановление через reversal + новый период, переоткрытия нет |
| Failed/void платёж | Период `PAID` врёт; нужно правило отката (OWNER) | Период `CLOSED` не трогается (evidence: SAP/Oracle); правило то
...[truncated 2963 chars]