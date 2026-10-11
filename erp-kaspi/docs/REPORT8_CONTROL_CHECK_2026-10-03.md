# REPORT_8 — проверка скрытых недостающих контрольных функций

**Дата:** 2026-10-03
**Статус:** read-only investigation. Код не изменён. REPORT_8 не выдуман.
**Вопрос:** есть ли в Master Prompt / FINANCE-OPS-001 контрольная функция, которая ТРЕБУЕТСЯ, но НЕ ИМЕЕТ никакой реализации — объективно доказуемо, без judgment call.

---

## 1. Что требует Master Prompt (контроль/мониторинг)

### 1.1. Раздел 32. FINANCIAL CONTROL CENTER

> «Создать/улучшить control layer:
>
> RED FLAGS:
> - sale without payment;
> - payment without sale;
> - payout without linked facts;
> - commission mismatch;
> - refund without original;
> - bank transaction without source;
> - duplicate external id;
> - unmatched payout;
> - negative material balance;
> - payroll without valid work entry;
> - work entry outside employee scope;
> - operation outside IP scope;
> - unexpected backdate;
> - reversal without relation;
> - posted operation mutation;
> - aggregate/detail mismatch.
>
> Каждый control:
> severity
> rule
> source
> affected object
> date
> owner
> status
> resolution.»

### 1.2. Раздел 15. RECONCILIATION

Требует reconciliation architecture со статусами MATCHED / PARTIAL_MATCH / MISMATCH / UNMATCHED / NEEDS_REVIEW / RESOLVED и полями expected / actual / difference / tolerance / reason / source ids / matched entity / reviewer / resolution / timestamps.

### 1.3. Раздел 31. DASHBOARD

Требует dashboard с метриками (turnover, revenue, expenses, direct cost, payroll, materials, Kaspi commissions, refunds, transfers, bank movement, cash movement, receivables, payables, net result, **unresolved mismatches**) и drill-down на каждую цифру.

### 1.4. FINANCE-OPS-001 §13. «КУДА ДЕЛИСЬ МОИ ДЕНЬГИ?»

> «Это **отдельная функция**.
> Пользователь выбирает: Today / This week / This month / Custom
> И система показывает:
> START BALANCE
> + SALES
> + OTHER INFLOWS
> + PAYOUTS
> − MATERIALS
> − PAYROLL
> − SUPPLIERS
> − COMMISSIONS
> − REFUNDS
> − OPERATING EXPENSES
> − TRANSFERS OUT
> = ENDING BALANCE
>
> Обязательно учитывай: transfer ≠ expense»

### 1.5. FINANCE-OPS-001 §14. DAILY / WEEKLY / MONTHLY REPORTING

Требует в отчётах: opening/closing balance, inflow/outflow, **unresolved discrepancies** (daily), **outstanding reconciliation** (monthly), drill-down на каждую цифру.

---

## 2. Маппинг 16 red flags §32 на реализацию

| # | Red flag | Статус | Где |
|---|---|---|---|
| 1 | sale without payment | **MISSING** | Нет query/view/endpoint |
| 2 | payment without sale | **MISSING** | Нет query/view/endpoint |
| 3 | payout without linked facts | PARTIAL | `PAYOUT_NOT_IN_LEDGER` в `v_kaspi_reconciliation` |
| 4 | commission mismatch | IMPLEMENTED | `COMMISSION_MISMATCH` в `v_kaspi_reconciliation` |
| 5 | refund without original | **MISSING** | Нет query/view/endpoint |
| 6 | bank transaction without source | **MISSING** | Нет query/view/endpoint |
| 7 | duplicate external id | GUARD ONLY | Unique constraints (`uq_orders_ip_external`, `uq_kaspi_payouts_external`, `uq_operations_ip_type_external`) — превентивно, но нет detective-отчёта |
| 8 | unmatched payout | PARTIAL | `NO_PAYOUT` в `v_kaspi_reconciliation` |
| 9 | negative material balance | **MISSING** | Нет check/view/endpoint |
| 10 | payroll without valid work entry | **MISSING** | Нет query/view/endpoint |
| 11 | work entry outside employee scope | GUARD ONLY | `ensure_employee_access()` в API — превентивно, нет detective-отчёта |
| 12 | operation outside IP scope | GUARD ONLY | `ensure_ip_access()` в API — превентивно, нет detective-отчёта |
| 13 | unexpected backdate | **MISSING** | Нет query/view/endpoint |
| 14 | reversal without relation | **MISSING** | (дополнительно BLOCKED by REVERSAL_Q1_Q7) |
| 15 | posted operation mutation | GUARD ONLY | DB-триггеры (`fn_protect_posted_operation`, ledger immutability) — превентивно, нет detective-отчёта |
| 16 | aggregate/detail mismatch | **MISSING** | Нет query/view/endpoint |

**Ключевой факт:** нигде в кодовой базе нет сущности «control» / «red flag» / «alert» как detective-слоя. Поиск по `app/`, `frontend/`, `database/schema.sql` не нашёл ни таблицы, ни view, ни endpoint, ни UI-модуля, реализующего §32 control layer с требуемыми полями (severity, rule, source, affected object, date, owner, status, resolution). Существующий `/monitoring/summary` — это operational monitoring (§72: 5xx errors, DB reachability, sync logs, auth failures), а не финансовый control layer.

В Requirements Matrix (98 REQ) **нет ни одного REQ**, отображающего Master Prompt §32.

---

## 3. Объективно доказуемо отсутствующие функции

### 3.1. §13 «КУДА ДЕЛИСЬ МОИ ДЕНЬГИ?» — ОТСУТСТВУЕТ ПОЛНОСТЬЮ

**Требование:** FINANCE-OPS-001 §13, цитата выше — «Это отдельная функция» с точной формулой START BALANCE → ENDING BALANCE и правилом transfer ≠ expense.

**Факт:** в кодовой базе нет:
- ни одного API endpoint для cash-flow;
- ни одного SQL view (`v_cash*` — поиск по `database/schema.sql` пуст);
- ни одной UI-функции (модуль «Банки» показывает только списки счетов, выплат и sync log).

**Вывод:** функция ТРЕБУЕТСЯ и НЕ РЕАЛИЗОВАНА. Это объективный gap, а не judgment call. Формула полностью задана требованием — реализация не требует выдумывания семантики.

### 3.2. §32 Financial Control Center — ОТСУТСТВУЕТ КАК СЛОЙ

**Требование:** Master Prompt §32, цитата выше — «Создать/улучшить control layer» с 16 red flags и структурой каждого control.

**Факт:** из 16 флагов полностью реализован как detective-контроль только 1 (commission mismatch), 2 частично, 5 существуют только как превентивные guards (не как отчёты), **8 отсутствуют полностью**. Единого control layer нет.

**Вывод:** слой ТРЕБУЕТСЯ и НЕ РЕАЛИЗОВАН. Это объективный gap. Отдельные флаги могут быть реализованы инкрементально без выдумывания семантики (правила заданы именами флагов).

### 3.3. §31 Dashboard — ОТСУТСТВУЕТ (с оговоркой)

**Требование:** Master Prompt §31 — dashboard с 15 метриками и drill-down.

**Факт:** во `frontend/` нет dashboard-модуля. Sidebar содержит ровно 10 модулей (§60): Операции, Импорт, Банки, Заказы, Товары, Материалы, Производство, Зарплата, Отчёты, Настройки. При этом REQ-0314 в Requirements Matrix заявляет «IMPLEMENTED — dashboard-модуль с period presets» — это **ложное заявление** в матрице.

**Оговорка:** §31 — управленческий инструмент, а не строго «контрольная функция». Но требуемая метрика «unresolved mismatches» — контрольная. Фиксирую как связанный gap + неточность в Requirements Matrix.

---

## 4. Что реализовано (для полноты картины)

| Требование | Реализация | Статус |
|---|---|---|
| §15 Reconciliation | `v_kaspi_reconciliation`, `reconcile_payout_full()`, `POST /kaspi/reconcile/{payout_id}` | TESTED (REQ-0307) |
| §72 Monitoring | `/monitoring/summary` | TESTED (REQ-0325) |
| §14 Daily/Weekly/Monthly | 7 отчётов + `/reports/ip-summary` | PARTIAL (нет unresolved discrepancies / outstanding reconciliation как отдельных функций) |
| §32 отдельные guards | Unique constraints, `ensure_*_access`, DB immutability triggers | IMPLEMENTED (превентивно) |

---

## 5. Итог

**Найдено 2 объективно отсутствующие требуемые контрольные функции:**

1. **§13 «КУДА ДЕЛИСЬ МОИ ДЕНЬГИ?»** (FINANCE-OPS-001) — cash-flow функция с полностью заданной формулой. Нет ни endpoint, ни view, ни UI.
2. **§32 Financial Control Center** (Master Prompt) — control layer с 16 red flags. Реализован 1/16 полностью, 8/16 отсутствуют полностью, единого слоя нет.

**Дополнительно:** REQ-0314 ложно заявляет о реализованном dashboard-модуле — dashboard отсутствует.

**Что это значит для REPORT_8:** ни одна из этих функций не является «8-м отчётом» в смысле REQ-0317 (отчёты — это `frontend/reports.py`). Но §13 cash-flow — сильнейший кандидат на объективно выводимую недостающую функцию: требование называет её «отдельной функцией» с точной формулой. Решение о том, считать ли её «8-м отчётом», остаётся за OWNER — но сам факт её отсутствия доказан объективно.

**Не требует решения владельца для начала работы:** формула §13 полностью задана; флаги §32 заданы именами. Можно реализовывать как обычные фичи по TDD.
