# Book gap analysis: Cash Flow, P&L, Balance, Accrual, Margin (book areas 1–5)

**Date**: 2026-10-03
**Book**: «Excel для финансового директора» (Актион, 2015, ~192 с.) — analyzed as a **management-accounting architecture source only**. Not a Kazakhstan regulatory source; no Excel formulas copied, no KZ tax/accounting rules invented.
**Book text**: `docs/research/book_excel_full.txt` (5429 lines, OCR of the full book; removed from git per commit fa731cf, kept locally).
**Scope**: book areas 1–5 vs erp-kaspi current implementation (HEAD at analysis time: `854cfad`).
**Companion analyses**: areas 6–10 (AR/AP, aging, dynamics, drill-down, treasury) and 11–16 (payment calendar, budget, overrun control, ЦФО, IP consolidation, unified register) are covered by sibling analyses — cross-referenced where they are implementation dependencies.

## Method

Each gap uses the required format: `BOOK EVIDENCE → CURRENT ERP → GAP → SEVERITY → PROPOSED REQUIREMENT → IMPLEMENTATION DEPENDENCIES`.
Book references are given as page numbers of the printed book (pp. 12–49 etc., from the OCR text).

## What the ERP already gets right (alignment, no action)

- **Single unified register** (book pp. 14–15: «реестр должен быть один и в него заносились абсолютно все операции, независимо от конкретного юрлица, расчетного счета, кассы… Не нужно плодить однотипные реестры») — `operations` table is exactly that: one central ledger across ИП, accounts, counterparties (`database/schema.sql:1490`).
- **Direct method cash flow** (book pp. 20–27: ОДДС built by articles via СУММЕСЛИМН) — `cash_flow()` is direct-method, article-based (schema.sql:2140). Alignment on method; gaps are structural (sections, extensibility).
- **Transfer ≠ expense** (book's investment/financial sectioning logic; §28) — enforced via `operation_types.affects_pnl=FALSE`.
- **Only executed transactions hit reports** (book p. 50: only «факт» status hits ОДДС/ОПУ formulas) — ERP's POSTED-only rule in `v_ip_financial_summary` and `pnl()` (schema.sql:2119, app/services/__init__.py:600).

---

## Area 1 — Cash Flow (ОДДС)

### GAP-1A: Cash-flow report has no operating / financing / investing sections

BOOK EVIDENCE: «Отчет состоит из трех разделов: операционная деятельность, финансовая деятельность и инвестиционная деятельность. Каждый раздел состоит из двух подразделов – поступления и платежи, а также строки сальдо» (p. 20). Articles are grouped by movement type, e.g. «Расчеты с площадками и клиентами», «Расчеты с персоналом», «Расчеты с бюджетом» (p. 20).

CURRENT ERP: `cash_flow(ip_id, from, to)` (`database/schema.sql:2140-2217`) returns a flat §13 list: start_balance, sales, other_inflows, payouts, materials, payroll, suppliers, commissions, refunds, operating_expenses, transfers_out, ending_balance. `GET /reports/cash-flow` (app/api/__init__.py:996) returns this flat shape. No sectioning exists.

GAP: Report structure does not separate operating / financial / investing activity; §13 is an answer to «КУДА ДЕЛИСЬ МОИ ДЕНЬГИ?» (a reconciliation question), not a full direct-method ОДДС.

SEVERITY: P2

PROPOSED REQUIREMENT: Add section grouping to the cash-flow output: each article mapped to one of {OPERATING, INVESTING, FINANCING} with per-section inflows / outflows / net and a total cash delta reconciled to start/end balance. Article→section mapping must be explicit and documented (METRIC ID per Master Prompt §11).

IMPLEMENTATION DEPENDENCIES: GAP-1B (article reference table with section attribute), or at minimum a documented static mapping.

### GAP-1B: Cash-flow articles are hardcoded in SQL, not a reference table

BOOK EVIDENCE: «Каждая строка отчета содержит свой уникальный пятизначный числовой код» (p. 20); справочник статей ДДС on a dedicated sheet, register rows reference it via data validation (pp. 17–19). New articles (e.g. амортизация for cost articles) are added to the reference list (p. 28).

CURRENT ERP: Articles (`SALE_KASPI_SHOP`, `EXPENSE_MATERIAL`, `SALARY_PAYMENT`, …) are `operation_types` rows (`database/schema.sql:2433`), but the cash-flow article set is hardcoded as `FILTER (WHERE code IN (...))` clauses inside `cash_flow()` (schema.sql:2185-2200). Adding a new report article requires changing the SQL function.

GAP: No `cash_flow_article` attribute/code on operation types or categories; no stable article codes; report extensibility requires DB function edits.

SEVERITY: P2

PROPOSED REQUIREMENT: Add explicit cash-flow article mapping: either a `cash_flow_article_code` + `cash_flow_section` column on `operation_types` (with Alembic migration + seed), or a dedicated `cash_flow_articles` reference table; rewrite `cash_flow()` to aggregate by that mapping.

IMPLEMENTATION DEPENDENCIES: None structural (migrations only). Owner decision on the article chart (which codes exist) — but note: defining article names is chart design, not KZ tax rule.

---

## Area 2 — P&L (ОПУ)

### GAP-2A: No P&L layer structure (маржинальный доход → операционная прибыль → чистая прибыль)

BOOK EVIDENCE: «в ОПУ вначале формируется маржинальный доход как разница между выручкой и прямыми затратами, затем из маржинального дохода вычитаются накладные расходы и формируется операционная прибыль, затем из операционной прибыли вычитается амортизация и формируется чистая прибыль как итог отчета» (p. 30). Rows carry numeric codes (e.g. строка ОПУ 7000 «Чистая прибыль», pp. 36–39).

CURRENT ERP: `v_ip_financial_summary` (`database/schema.sql:2097-2121`) and `pnl()` (app/services/__init__.py:600) produce only total_income / total_expense / net_profit — a flat P&L with no layers. `GET /pnl` (app/api/__init__.py:1729) mirrors it.

GAP: No marginal income (выручка − прямые затраты), no operating profit (minus overhead), no net profit decomposition. The book's layered P&L is entirely absent.

SEVERITY: P1

PROPOSED REQUIREMENT: Build a layered P&L: revenue → direct costs → **marginal income** → overhead → **operating profit** → depreciation/other → **net profit**, per month (accrual basis, see GAP-4A), per ИП, with formal METRIC ID definitions (Master Prompt §11).

IMPLEMENTATION DEPENDENCIES: GAP-2B (direct vs overhead classification — see OQ-010 overhead/costing companion area); GAP-4A (accrual_date — P&L must be accrual-based); owner decision on which expense types are «прямые» vs «накладные» (business semantics, not to be invented).

### GAP-2B: No direct vs overhead expense classification

BOOK EVIDENCE: The whole P&L layer chain (p. 30) depends on distinguishing прямые затраты from накладные расходы. The book's chart of «Статьи оборотов» separates cost articles from ДДС articles (p. 28).

CURRENT ERP: `operation_types` (`database/schema.sql:784-807`) has `kind` (INCOME/EXPENSE/TRANSFER) and `affects_pnl` only. `categories` (`database/schema.sql:2446`) lists expense categories (materials, delivery, salary, rent, utilities, advertising…) with no direct/overhead attribute. Nothing classifies an expense as direct vs overhead.

GAP: Classification needed for маржинальный доход cannot be expressed in the data model.

SEVERITY: P1

PROPOSED REQUIREMENT: Add `cost_nature` (DIRECT / OVERHEAD / N/A) to `categories` (or `operation_types`) with migration + documented default mapping; the layered P&L consumes it.

IMPLEMENTATION DEPENDENCIES: Owner decision on the direct/overhead mapping (OQ-010 overhead/costing — companion analysis). Do NOT invent.

---

## Area 3 — Management balance

### GAP-3A: No management balance at all

BOOK EVIDENCE: «для получения баланса нам даже не придется производить никаких изменений в реестре… все необходимые данные мы возьмем из имеющихся у нас отчетных форм. А именно – из отчета о движении денежных средств и отчета о прибылях и убытках» (p. 32). Balance inputs: «баланс на начало года; отчет о прибылях и убытках за период с начала года по отчетную дату; отчет о движении денежных средств за период с начала года по отчетную дату» (p. 35). Article formulas: Денежные средства = opening + inflows − outflows (p. 33); AR/AP as unified «сальдо взаиморасчетов» = opening + accruals − payments (p. 33–34); ОС/НМА = opening + purchases − sales − depreciation (p. 34); Кредиты и займы = opening + received − repaid (p. 35); Собственный капитал = opening + profit + shareholder inflows − outflows (p. 35).

CURRENT ERP: No balance view, table, function, or endpoint exists. Master Prompt §11 lists Receivable/Payable/Inventory/Bank balance as concepts to distinguish; §31 lists them as dashboard KPIs. TD-024 documents the gap.

GAP: Management balance is entirely absent. The book's key architectural insight — balance is *derived* from P&L + cash flow + opening balance, requiring no extra register columns — is not implemented.

SEVERITY: P1

PROPOSED REQUIREMENT: Build a management balance report: per ИП, per reporting date; articles: cash, AR/AP saldo, fixed assets, loans, equity; each article = opening (year start) + YTD movement from P&L/cash-flow layers. Formal METRIC ID definitions per Master Prompt §11.

IMPLEMENTATION DEPENDENCIES: GAP-3B (opening balances storage); GAP-1A/GAP-2A (sectioned cash flow + layered P&L as data sources); GAP-4A (accrual_date for the AR/AP saldo formula). Note the book's fixed-asset and loan articles require purchase/sale/loan operation types that partially exist (`operation_types` has no loan types) — scope to be defined by owner.

### GAP-3B: No opening-balance storage

BOOK EVIDENCE: «Долг на начало года… в отчет придется вводить вручную: в реестре такой информации нет… эту информацию надо внести только один раз» (p. 41); «В баланс, для его корректной работы, надо часть информации вносить вручную» (p. 35); «остаток денежных средств на начало года», «сальдо взаиморасчетов на начало года» (pp. 33–34).

CURRENT ERP: No table/endpoint for opening balances. `cash_flow()` computes start_balance by summing all prior POSTED operations (schema.sql:2172-2178) — which only works if the full history is in the DB.

GAP: No mechanism to enter opening balances once per year per ИП/article; the balance cannot be anchored without complete historical data.

SEVERITY: P1 (blocks GAP-3A)

PROPOSED REQUIREMENT: Add `opening_balances` table (ip_id, article/code, balance_date, amount) + API for one-time entry + Alembic migration; balance report consumes it. The book's «внести один раз» rule applies.

IMPLEMENTATION DEPENDENCIES: Owner decision on the article chart for opening balances (cash accounts, AR/AP, loans, equity, fixed assets).

---

## Area 4 — accrual_date vs payment_date

### GAP-4A: operations has no accrual date — accrual accounting is impossible

BOOK EVIDENCE: «правильным решением будет отражение операций начисления доходов и расходов в том же реестре… добавить графу «Дата начисления». Затем переименовать графу «Дата операции» в «Дата оплаты» и добавить графу «Месяц начисления»» (p. 28). «одна из граф «Дата оплаты» и «Дата начисления» может быть пустой» (p. 29) — a pure cash op has no accrual date, a pure accrual op has no payment date; the salary example shows three register rows capturing six business operations (three accruals + three payments, pp. 29–30). «Формула СУММЕСЛИМН в ОПУ ссылается не на месяц оплаты в реестре, а на месяц начисления расходов» (p. 30) — **ОДДС aggregates by payment month, ОПУ by accrual month**.

CURRENT ERP: `operations` has a single `operation_date DATE NOT NULL` (`database/schema.sql:1497`). Both `v_ip_financial_summary` and `cash_flow()` aggregate by `operation_date`. There is no accrual concept anywhere in the schema (`grep -i accrual database/schema.sql` → empty).

GAP: The book's foundational distinction — when income/expense is *recognized* vs when cash *moves* — cannot be expressed. Current P&L is effectively cash-basis: salary paid in February for January work lands in February's P&L. The book's core management-accounting pattern (single register, two dates) is missing.

SEVERITY: P1

PROPOSED REQUIREMENT: Add `accrual_date DATE NULL` to `operations` (Alembic migration + index); P&L aggregations (`v_ip_financial_summary`, `pnl()`) switch to accrual-date basis; cash-flow keeps `operation_date` basis; define and document the recognition rule for rows where `accrual_date` is NULL.

IMPLEMENTATION DEPENDENCIES: **Owner decision required** on the NULL rule — options: (a) book-style: NULL accrual_date = no P&L recognition (pure cash movement); (b) default accrual_date = operation_date (cash-basis fallback). This is business semantics — do NOT invent. Also requires backfill policy for existing rows (owner decision: treat all historical `operation_date` as accrual date, or leave NULL).

### GAP-4B (consequence): AR/AP saldo formula cannot be implemented without GAP-4A

BOOK EVIDENCE: «Сальдо взаиморасчетов на 1 февраля = Сальдо взаиморасчетов на 1 января + Начисление за январь – Оплата за январь» (p. 33–34). The book unifies AR/AP: «для управленческого учета дебиторская и кредиторская задолженность – это единая категория (можно объединить эти два понятия единым термином «сальдо взаиморасчетов»)».

CURRENT ERP: TD-024 documents that receivables/payables are not tracked at all.

GAP: Recorded for dependency traceability: GAP-3A (balance AR/AP article) and TD-024 both depend on GAP-4A. No separate requirement proposed here — see companion analysis (areas 6–10) and TD-024.

SEVERITY: P1 (inherited)

PROPOSED REQUIREMENT: (See TD-024 / areas 6–10 analysis.)

IMPLEMENTATION DEPENDENCIES: GAP-4A, GAP-3B.

---

## Area 5 — Margin layers (маржинальный доход → операционная прибыль → чистый результат)

Covered structurally by GAP-2A; this section records the book's margin semantics as evidence.

BOOK EVIDENCE: «в ОПУ вначале формируется маржинальный доход как разница между выручкой и прямыми затратами, затем из маржинального дохода вычитаются накладные расходы и формируется операционная прибыль, затем из операционной прибыли вычитается амортизация и формируется чистая прибыль как итог отчета» (p. 30). Also: «центр маржинального дохода, центр прибыли» as ЦФО types (p. 70) — margin responsibility is an organizational concept too.

CURRENT ERP: `order_gross_profit()` (app/services/__init__.py:181) computes per-order gross profit/margin — the only margin concept in the codebase. No company-level margin layers exist.

GAP: No marginal income / operating profit / net profit chain at company (ИП) level. The per-order gross margin does not roll up into a management P&L.

SEVERITY: P1

PROPOSED REQUIREMENT: (Same as GAP-2A.) The layered P&L must define each layer as a METRIC ID (Master Prompt §11: «Нельзя иметь один и тот же термин с двумя различными формулами»), including margin-percentage rows.

IMPLEMENTATION DEPENDENCIES: GAP-2A, GAP-2B, GAP-4A.

---

## TD-023 — Does the book define «оборот» / turnover?

**Finding: No. The book does not define «оборот» as a management KPI.**

Evidence of the word's usage in the book:
- Account turnovers (бухгалтерское значение): «обороты по дебету и кредиту», «Суммарные обороты по всем счетам» (p. 64) — the classic Russian accounting meaning, unrelated to a KPI.
- Company size: «полнофункциональный управленческий учет построить на базе Excel реально лишь для небольших компаний (с оборотом до 300 млн рублей в год…)» (p. 12).
- Revenue-like base: «Заложим на эти цели 1,5 процента от оборота» (p. 126, personnel-cost model) — used loosely as a revenue base, never formally defined.

The book's formal revenue term is **выручка** (accrual-based, recognized by месяц начисления, p. 30), and its formal flow terms are обороты по счету. There is no «оборот» KPI definition to adopt.

**Conclusion**: TD-023 remains **OWNER DECISION REQUIRED** (Master Prompt §11: «Нельзя иметь один и тот же термин с двумя различными формулами»; §11 explicitly lists turnover for special attention). Candidate definitions to put to the owner: (a) turnover = total income (выручка, accrual basis); (b) turnover = gross cash inflow (including payouts, excl. transfers); (c) turnover = sum of |amount| of INCOME-kind operations. Do NOT invent — TD-023 stays open.

## TD-022 — What does the book recommend for management dashboards?

**Finding: The book has no dashboard concept as a distinct artifact; its recommendation is charts + analytical reports.**

- «максимально активно использовать диаграммы вместе с отчетами, особенно детализированными, так как диаграммы воспринимаются значительно легче и использовать их для анализа финансово-экономической деятельности компании гораздо удобнее. Особенно хороши диаграммы в отчетах, содержащих сотни однотипных показателей (например… ежедневную динамику каких-либо показателей)» (pp. 48–49, «Отчет с диаграммой»).
- Additional reports principle: top-level reports are «слишком укрупнены и обезличены»; analytics come from single-dimension reports (по контрагентам/продуктам/отделам продаж, p. 40).
- The book's «drill-down» (кнопка «Детализация»: click a report cell → filtered register rows, pp. 25–26) is the interactive layer — see companion analysis area 9.

**Conclusion**: The book supports a dashboard as *top-level reports + analytical breakdowns + charts with daily dynamics*, but provides **no KPI list or layout** — TD-022 remains **owner layout decision** (Master Prompt §31 KPI set: Sales/Refunds/Revenue/Expenses/Payroll/Materials/Commissions/Transfers/Net cash movement + period filters + drill-down). The book adds one evidence-based UI guideline: pair every detailed report with a chart, and make the chart the daily-dynamics view.

---

## Gap summary table

| ID | Gap | Severity | Owner decision needed? |
|---|---|---|---|
| GAP-1A | Cash flow: no operating/financing/investing sections | P2 | Article chart design |
| GAP-1B | Cash-flow articles hardcoded in SQL | P2 | Article chart design |
| GAP-2A | No layered P&L (margin → operating → net) | P1 | Direct/overhead mapping (OQ-010) |
| GAP-2B | No direct vs overhead classification in data model | P1 | Direct/overhead mapping (OQ-010) |
| GAP-3A | No management balance | P1 | Article chart for balance |
| GAP-3B | No opening-balance storage | P1 | Article chart for opening balances |
| GAP-4A | No accrual_date on operations | P1 | NULL recognition rule + backfill policy |
| GAP-4B | AR/AP saldo blocked on GAP-4A (→ TD-024) | P1 | (via TD-024) |
| Area 5 | No company-level margin chain | P1 | (via GAP-2A) |
| TD-023 | «Оборот» undefined in book → owner decision | P1 | Turnover formula (A/B/C) |
| TD-022 | Book: charts + analytics, no KPI list → owner layout | P1 | Dashboard layout |

## Recommended implementation order (dependency order)

1. **GAP-4A** (`accrual_date`) — foundational; unlocks accrual P&L, AR/AP, balance.
2. **GAP-2B** (direct/overhead classification) — after OQ-010 owner decision.
3. **GAP-2A + Area 5** (layered P&L) — on top of 1+2, accrual basis.
4. **GAP-1B → GAP-1A** (article reference table → sectioned cash flow).
5. **GAP-3B → GAP-3A** (opening balances → derived management balance).

Each step: Alembic migration → service/view → API → tests → docs → commit (post-change protocol).

## Cross-references

- TD-022, TD-023, TD-024, TD-025 (`docs/TECHNICAL_DEBT.md`) — this analysis provides book evidence for TD-022/TD-023 and dependency detail for TD-024.
- OQ-010 overhead/costing — companion analysis (areas 6–10 / 11–16); owns the direct/overhead semantic decision (GAP-2B dependency).
- Treasury statuses (зарег./план/факт, book p. 50) — companion analysis area 10; relevant to cash-flow report filtering (GAP-1A).
- Drill-down (book pp. 25–26) — companion analysis area 9; relevant to TD-022 dashboard.
- Master Prompt §11 (CASH VS PROFIT, METRIC ID rule, «one term, one formula»): `~/workspace/user/files/Project_PromPt.txt:483-530`.
- Current cash flow: `database/schema.sql:2127-2217`, `app/api/__init__.py:996-1024`, `tests/test_cash_flow.py`.
- Current P&L: `database/schema.sql:2090-2124`, `app/services/__init__.py:600-657`, `app/api/__init__.py:1729-1747`.
