# REPORT_8 — расследование: можно ли объективно вывести 8-й отчёт

Дата: 2026-10-03. Статус: **OWNER DECISION REQUIRED** — объективное выведение невозможно.
Read-only расследование; код не изменён.

## 1. Семь существующих отчётов (`frontend/reports.py`, функция `render()`)

Опции селектора (i18n-ключи `report_opt_*` в `frontend/app.py`, строки 253–259 / 441–447):

| # | Отчёт | Источник данных | i18n-заголовок |
|---|-------|-----------------|----------------|
| 1 | Финансовый итог по ИП | `GET /reports/ip-summary` (`v_ip_financial_summary`) — месяц, доходы, расходы, чистая прибыль, переводы in/out, число операций | «Финансовый итог по ИП (§63 v_ip_financial_summary)» |
| 2 | Сверка Kaspi | `GET /reports/kaspi-reconciliation` | «Сверка Kaspi (§63 v_kaspi_reconciliation)» |
| 3 | Зарплата | `GET /reports/payroll-due` (`v_payroll_due`) — сотрудник, период, начислено/выплачено/к выплате | «Зарплата к выплате (§63 v_payroll_due)» |
| 4 | Себестоимость заказов | `GET /reports/order-costing` (`v_order_costing`) | «Себестоимость заказов (§63 v_order_costing)» |
| 5 | Журнал операций | `GET /operations` — read-only журнал | «Журнал операций (§16 — только чтение)» |
| 6 | Материалы | `GET /materials` — реестр материалов | «Материалы (§48 реестр)» |
| 7 | Выработки | `GET /reports/work-entries` (`v_work_entries_detail`) | «Выработки (§63 v_work_entries_detail)» |

Каждый — с empty state, §15-форматом сумм и Excel-экспортом сырых чисел (§62/DEF-006, ADR-013: PDF не реализуется).
Дополнительные API-эндпоинты `/reports/work-entries/summary` и `/reports/payroll-due/summary`
(`app/api/__init__.py`, строки 871, 907) — это KPI-агрегаты для других модулей, а не отдельные отчёты.

## 2. Что говорят документы — дословные цитаты

**`docs/REQUIREMENTS_MATRIX.md:118` (REQ-0317)** — единственный источник числа «8»:
> `| REQ-0317 | A | 8 отчётов + экспорт Excel | 62 | ШАГ 11 | PARTIALLY IMPLEMENTED — frontend/reports.py (7 отчётов + Excel-экспорт; 8-й отчёт не определён — BLOCKED, требуется решение владельца) |`

Перечисления восьми отчётов в строке нет — только число.

**`docs/PROJECT_PLAN.md:134`** (DAY 13 — ШАГ 10–11):
> `- Goal: dashboard (оборот/выручка/расходы/прибыль/зарплата/себестоимость), фильтры периодов, 10 модулей sidebar, 8 отчётов, экспорт Excel.`

Число без перечисления.

**Цепочка §62.** Заголовок матрицы: «Источник: MASTER PROMPT v3.0». Колонка § ссылается на нумерацию
MASTER PROMPT v3.0, где §62 = REPORTS, §63 = DATABASE VIEWS (ср. заголовки отчётов в `frontend/app.py`,
ссылающиеся на «§63 v_*»). Текст MASTER PROMPT v3.0 в репозитории **отсутствует**; есть только аудит
`docs/FULL_AUDIT_MASTER_PROMPT_v3_0.txt:849`:
> `§62 — REPORTS / Источник: строки 1477–1496 / Вердикт: P1 / ПРОБЛЕМА: Reports не определяют data source, as-of date, rounding, permissions и export audit. / РЕШЕНИЕ: Добавить report contracts.`

Аудит не цитирует перечень отчётов — в нём §62 про контракты отчётов, а не про их список.

**Текущий Master Prompt (`~/workspace/user/files/Project_PromPt.txt`, 59 разделов)** — перечня 8 отчётов нет.
Релевантные, но не детерминирующие разделы: §31 DASHBOARD (список метрик, включая bank/cash movement,
receivables/payables — «Показывать при необходимости»), §32 FINANCIAL CONTROL CENTER (16 red flags).

**`~/workspace/user/files/FINANCE-OPS-001.txt`** — перечня 8 отчётов нет. Релевантное:
- §13 «КУДА ДЕЛИСЬ МОИ ДЕНЬГИ?» (строки 387–412): «**Это отдельная функция.**» Пользователь выбирает
  период; система показывает START BALANCE + SALES + OTHER INFLOWS + PAYOUTS − MATERIALS − PAYROLL −
  SUPPLIERS − COMMISSIONS − REFUNDS − OPERATING EXPENSES − TRANSFERS OUT = ENDING BALANCE.
- §14 DAILY/WEEKLY/MONTHLY REPORTING (строки 420–451): состав daily (opening balance, inflow, outflow,
  sales, refunds, commissions, payroll, materials, transfers, closing balance, unresolved discrepancies)
  и monthly (revenue, expenses, direct costs, payroll, materials, commissions, refunds, net result,
  **cash movement**, **outstanding reconciliation**). Это состав показателей, а не список отчётов.
- §15 PROFIT ≠ CASH (строки 455–470): отдельно показывать Revenue / Expenses / Profit / Cash inflow /
  Cash outflow / Cash balance.

**Git-история:** коммит `865f461` (2026-10-02) исправил ложное «IMPLEMENTED — 8 отчётов» на честное
«PARTIALLY IMPLEMENTED — 7 отчётов; 8-й не определён — BLOCKED». Ни одна ревизия не содержала перечня.

## 3. Анализ: есть ли объективно недостающий отчёт?

Проверено покрытие 7 отчётов по областям требований: P&L по ИП (1), сверка Kaspi (2), зарплата (3),
себестоимость (4), журнал операций (5), материалы (6), выработки (7).

Наблюдаемые пробелы — и почему ни один не детерминирован как «8-й»:

1. **Движение денег / «Куда делись мои деньги».** §13 описывает это как «отдельную функцию», а не как
   8-й отчёт REQ-0317. §15 требует разделять profit и cash; отчёт №1 — P&L-ориентирован (доходы/расходы/
   прибыль + переводы), cash inflow/outflow/balance отдельным разрезом не показывает. Сигнал сильный,
   но привязка «§13 = 8-й отчёт» — вывод, а не цитата.
2. **Финансовый контроль (red flags).** Master Prompt §32 требует control layer с 16 проверками
   (severity, rule, source, affected object, date, owner, status, resolution). Отдельного отчёта среди
   7 нет; но существует `/monitoring/summary` (REQ-0325) — операционный мониторинг, не бизнес-контроль.
   Отождествлять §32 с REQ-0317 нельзя: это разные требования.
3. **Outstanding reconciliation.** §14 monthly требует «outstanding reconciliation»; отчёт №2 покрывает
   сверку Kaspi, но отдельный отчёт по незакрытым расхождениям/aging не выделен. Однако это
   детализация существующего отчёта №2, а не очевидно «новый» отчёт.
4. **Дебиторка/кредиторка.** Master Prompt §31 и FIN-OPS-001 §12 упоминают receivables/payables как
   метрики «при необходимости». Семантика «что считать задолженностью» в требованиях не определена
   (продажа без оплаты? payout позже заказа?) — выдумывать запрещено.
5. **Налоговая сводка.** `tax_rates_history` существует (REQ-0219), но налоговый режим ИП неизвестен
   (риск R-04 в PROJECT_PLAN.md) — построение отчёта потребовало бы выдуманных правил.

**Вывод:** ни одно требование не говорит «отчётов должно быть 8, и вот их список» или «8-й — это X».
Число «8» в REQ-0317 — без перечисления; исходный текст §62 MASTER PROMPT v3.0 утрачен. Любой выбор
8-го отчёта был бы изобретением, что запрещено. Статус: **OWNER DECISION REQUIRED**.

## 4. Кандидаты для решения владельца (без выбора)

| Кандидат | Что показывал бы | Какую область требований закрывает | Оговорка |
|----------|------------------|-------------------------------------|-----------|
| A. «Движение денег» (cash flow по §13) | Период → START BALANCE + inflows (sales, other, payouts) − outflows (materials, payroll, suppliers, commissions, refunds, operating expenses, transfers out) = ENDING BALANCE | FINANCE-OPS-001 §13, §15, §14 daily | §13 назван «отдельной функцией», не отчётом; дашборд (REQ-0314) частично показывает движение |
| B. «Финансовый контроль» (red flags) | 16 проверок Master Prompt §32: sale without payment, payout without linked facts, commission mismatch, duplicate external id, negative material balance, aggregate/detail mismatch и др. — с severity/status/resolution | Master Prompt §32 | Пересекается с `/monitoring/summary` (REQ-0325); потребует решения, что дублировать |
| C. «Незакрытые расхождения сверки» | Outstanding reconciliation с aging: unmatched payouts, amount/commission mismatch, unexplained payments, unresolved discrepancies (daily §14) | FINANCE-OPS-001 §14, §20; REQ-0307 | Может рассматриваться как детализация существующего отчёта №2, а не новый отчёт |
| D. «Дебиторская/кредиторская задолженность» | Receivables/payables в разрезе контрагентов и ИП | Master Prompt §31, FINANCE-OPS-001 §12 | Семантика задолженности не определена в требованиях — владельцу придётся определить правила |

Налоговая сводка сознательно не включена в кандидаты: налоговый режим ИП неизвестен, правила
пришлось бы выдумывать (нарушение базового запрета).

## 5. Рекомендуемый следующий шаг

Зафиксировать выбор владельца (A/B/C/D или свой вариант с определением состава) — только после этого
проектировать контракт отчёта (data source, as-of date, rounding, permissions, export audit — по
требованию аудита §62 REPORTS) и реализовывать по циклу TDD → implementation → verification → commit.
