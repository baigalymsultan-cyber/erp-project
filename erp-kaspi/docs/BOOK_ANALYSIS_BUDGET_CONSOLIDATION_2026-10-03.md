# Book gap analysis: Budget, Cost Centers, Consolidation, Operations Registry (book areas 12–16) + OQ-010

Source: `docs/research/book_excel_full.txt` — "Excel для финансового директора" (2015), Russian management-accounting patterns ONLY. Not a KZ regulatory source. Patterns extracted, not Excel mechanics copied.

Compared against: `~/workspace/erp-kaspi` @ `b7437b6` (fin-ops-001). All CURRENT ERP facts verified by grep on 2026-10-03.

Related open items: TD-022 (dashboard, §31, P1), TD-023 (turnover undefined, P1), TD-024 (AR/AP not tracked, P1), TD-025 (METRIC IDs, P2), REQ-0314 (Financial Control Center — NOT IMPLEMENTED), §32 Financial Control Center (open).

---

## Area 12. Budget vs Actual (план-фактный анализ)

BOOK EVIDENCE: Ch 2 «Интеграция бюджетирования с управленческим учетом и план-фактный анализ». «Типовой алгоритм бюджетирования»: БДДС (бюджет движения денежных средств), БДР (бюджет доходов и расходов), ББЛ (бюджет по балансовому листу). Five-stage process: «Этап 1. Формирование и утверждение бюджетов ЦФО → Этап 2. Формирование и утверждение консолидированного бюджета → Этап 3. Контроль платежей → Этап 4. Оперативная корректировка бюджетов → Этап 5. План-фактный анализ». Key mechanism: «обе эти системы реализованы в Excel, на основе реестров операций и учетных политик с одинаковыми правилами отражения хозяйственных операций в учетных регистрах... присутствуют два реестра: фактических транзакций и плановых транзакций. Причем указанные реестры можно сравнивать между собой». Report shape: rows = budget articles, columns per month = «План» / «Факт» / «Отклонения» where «Отклонения» = (fact−plan)/plan with zero-division guard. Scenarios + versions: «Версий бюджета может быть много, так как в него должна вноситься любая новая информация, которая влияет на будущие доходы и расходы компании».

CURRENT ERP: `app/models/finance.py`, `database/schema.sql`, `app/api/__init__.py`, `frontend/` — zero budget constructs. `grep -rni "budget" app/ database/ frontend/` returns nothing (only rate-limit internals in `.venv`). `REQUIREMENTS_MATRIX.md` has no budget requirement; `FINANCE-OPS-001.txt` contains no «бюджет». No plan registry, no scenarios/versions, no plan-fact report.

GAP: No plan data exists at all, so plan-fact comparison is impossible. This is also the missing analytical core behind REQ-0314/§32 (Financial Control Center) and TD-022 (§31 dashboard).

SEVERITY: P2

PROPOSED REQUIREMENT: Introduce a plan registry mirroring the operations registry: `budget_versions` (id, scenario, version_no, period_from, period_to, status DRAFT/APPROVED) + `budget_lines` (version_id, ip_id, type_id/article, month, amount, cfo_id nullable); plan-fact report = budget_lines vs `operations` aggregates per article × month with deviation %; support ≥2 scenarios and ≥2 versions per the book. Mark as DEFERRED requirement candidate — no FINANCE-OPS-001 section mandates budgeting; needs owner approval before schema work.

IMPLEMENTATION DEPENDENCIES: owner decision that budgeting is in scope (no § reference exists); budget article dimension must equal `operation_types`/`categories` for plan-fact join to work (design decision); optional ЦФО dimension (area 14).

---

## Area 13. Budget overrun control (контроль платежей / перерасход)

BOOK EVIDENCE: Этап 3: «Контроль платежей. После того как все бюджеты синхронизированы и утверждены, начинается процесс их исполнения... в соответствии с рамками, прописанными в бюджете. А все превышения заложенных в бюджете сумм согласовываются руководством компании в установленном порядке». Treasury automation: transactions carry statuses «зарег.» (planned, not approved) → «план» (approved, will happen on set date) → «факт» (executed). Overrun check via a «Перерасход» column: when status = «план», if (fact month+article + this payment) > budget month+article → «перерасход», else «ок»; «сотрудник финансовой службы, ответственный за контроль соответствия расходов бюджету, может принять меры (отменить оплату света, запросить комментарии от инициатора платежа и т.п.)».

CURRENT ERP: No budget → no overrun check possible. What prevents overspending today: nothing proactive. `GET /reports/cash-flow` (app/api/__init__.py:996, `cash_flow()` in schema.sql:2140) is retrospective; `pay_payroll_summary` (app/services/__init__.py:396) has the §57 double-payment guard + `fn_payroll_payment_no_overpay()` trigger (migration 0013) which blocks overpayment of a payroll summary, not budget overrun; transfers enforce `amount > 0` only. Operation statuses are POSTED/REVERSED/CORRECTED/DRAFT (app/models/finance.py:151-154) — no registered/planned/fact treasury statuses.

GAP: No pre-payment budget-limit check; no planned/registered transaction statuses; no overrun flag or approval escalation.

SEVERITY: P2

PROPOSED REQUIREMENT: Treasury workflow on top of the operations registry: new lifecycle statuses (e.g. REGISTERED → PLANNED → POSTED) — explicitly NOT reusing the frozen financial statuses (REVERSAL_Q1_Q7 freeze); on PLANNED→POSTED transition, check cumulative month+article spend against the approved budget (area 12); overrun → payment blocked pending approval by an authorized role (RBAC, relates to OQ-011). This is a design proposal only; do not implement before the budget model (area 12) exists.

IMPLEMENTATION DEPENDENCIES: area 12 budget model; REVERSAL_Q1_Q7 answers (status semantics frozen); approval-role definition (OQ-011).

---

## Area 14. Cost centers / ЦФО

BOOK EVIDENCE: ЦФО = «подразделение, которое выполняет обособленные [функции]...» classified by dominant flow: «центр доходов, центр расходов, центр маржинального дохода, центр прибыли, центр инвестиций и т.д.»; «Каждый ЦФО получает доходы и осуществляет расходы по определенным статьям БДР и БДДС, набор этих статей и формирует его бюджет». System requirement: «Система бюджетирования должна уметь формировать бюджеты центров финансовой ответственности (БДДС и БДР) и консолидировать их в общий бюджет компании». Registry design: «если нужна детализация по ЦФО – реквизит «ЦФО»» (ch 1, registry attributes rule). Plan-fact «на уровне ЦФО» for distributed budget control by ЦФО heads.

CURRENT ERP: No subdivision below ИП. `app/models/hr.py:96` — `employees.assigned_ip_id` only; no department/cost-center table. `operations` has `ip_id` but no `cfo_id` dimension; `idx_operations_analytics (ip_id, operation_date, type_id)` (app/models/finance.py:164) carries no ЦФО. No ЦФО-filtered reports.

GAP: No cost-center dimension in registry, models, or reports. Book's ЦФО-budget → consolidation pipeline cannot be mapped onto erp-kaspi without it.

SEVERITY: P2

PROPOSED REQUIREMENT: `cost_centers` reference table (id, ip_id, name, cfo_type ∈ {REVENUE, COST, MARGIN, PROFIT, INVESTMENT} per the book's taxonomy); nullable `cfo_id` FK on `operations` (and optionally `employees`, `budget_lines`); reports accept `cfo_id` filter; extend `idx_operations_analytics`. Owner must first define the actual organizational structure — do not invent ЦФОs.

IMPLEMENTATION DEPENDENCIES: owner decision on org structure (which ЦФОs exist); migration + audit-trigger coverage per the A-2/A-3/A-4 pattern (migration 0010); area 12 if ЦФО budgets are wanted.

---

## Area 15. Multi-IP consolidation

BOOK EVIDENCE: Two consolidation ideas. (1) Single registry across legal entities: «реестр должен быть организован таким образом, чтобы он был один и в него заносились абсолютно все операции, относящиеся к компании, независимо от конкретного юрлица, расчетного счета, кассы и т.д. Не нужно плодить однотипные реестры, это сильно усложняет работу с ними». (2) Consolidated budget = «сумма всех бюджетов ЦФО, из которой исключены внутрифирменные обороты (если они есть)»; consolidation builds a сводный реестр with a ЦФО column and aggregates.

CURRENT ERP: (1) Satisfied — single `operations` table with `ip_id` (app/models/finance.py:144). (2) Partially: `GET /pnl` accepts an IP scope list (app/api/__init__.py:~1730, `ip_scope`) and sums across IPs; `transfers` post TRANSFER_OUT + TRANSFER_IN legs and §63 excludes transfer types from P&L — the book's "исключены внутрифирменные обороты" for cash movements. Gaps: `GET /reports/cash-flow` is single-IP only (`ip_id` required, app/api/__init__.py:1001); `GET /reports/ip-summary` lists per-IP rows with no consolidated total; no elimination rule exists for inter-IP non-cash turnovers (e.g. inter-IP sales) — the model has no concept of them.

GAP: No labeled consolidated report; cash-flow cannot span IPs; inter-IP elimination defined only for cash transfers, not for any inter-IP revenue/expense turnovers.

SEVERITY: P3

PROPOSED REQUIREMENT: (a) Document `GET /pnl` multi-IP scope as the consolidation path or add `GET /reports/consolidated-pnl`; (b) extend `cash_flow()` to accept an IP list; (c) owner confirms whether inter-IP sales exist — if yes, define an elimination rule (new requirement); if no, document that transfer exclusion (§63/§76) is the complete elimination mechanism.

IMPLEMENTATION DEPENDENCIES: owner confirmation on inter-IP sales; minor SQL work for (b).

---

## Area 16. Unified operations registry — is `operations` really the single source?

BOOK EVIDENCE: «основным источником информации для управленческих отчетов является реестр хозяйственных операций»; the registry «должен быть один и в него заносились абсолютно все операции, относящиеся к компании». The book's registry carries both accrual and payment dates per transaction (ch 1: графы «Месяц оплаты» / «Месяц начисления»), and statuses зарег./план/факт.

CURRENT ERP: `operations` is the single source for P&L (`pnl()`, §63) and cash-flow (`cash_flow()`, §13) — one table, `ip_id`-scoped, DELETE forbidden (§27). BUT these are captured elsewhere with NO operation legs:
1. **Payroll accrual**: `calculate_payroll` (app/services/__init__.py:233) creates `payroll_summary` rows and zero operations — salary expense reaches P&L only when `pay_payroll_summary` posts SALARY_PAYMENT (cash basis for salaries; no accrual leg).
2. **Kaspi payouts**: `post_kaspi_payout` (app/api/__init__.py:1700) stores the `kaspi_payouts` envelope, creates zero operations — payout cash arrival enters the registry only via manual `add_operation`.
3. **Orders**: `orders`/`order_items`/`order_materials` create zero operations — revenue at shipment and material costs are not auto-posted (material purchases via ETL/manual `add_operation`).
4. Single date only: `operations` has `operation_date` (app/models/finance.py) — no `accrual_date` vs `payment_date` split, unlike the book's registry.

GAP: Registry is single-source for reports but incomplete — accruals, payout envelopes, and order economics live outside it; no accrual/payment date distinction.

SEVERITY: P1 (for payroll accrual: salaries hit P&L at payment, not when earned — conflicts with the book's ОПУ accrual logic and with area 4 accrual_date vs payment_date; needs a requirements check against §34–§36 before changing)

PROPOSED REQUIREMENT: (a) Decide payroll posting basis: if accrual required, `calculate_payroll` must post a SALARY_ACCRUAL expense operation (accrual_date = period end) and `pay_payroll_summary` settles it (payment_date) — requires new operation types and a §34–§36/§63 requirements review, owner-confirmed; (b) payout posting: auto-create PAYOUT_IN operation on payout receipt (or document manual flow); (c) add `accrual_date` nullable to `operations` (default = operation_date), used by P&L, `operation_date`/`payment_date` by cash-flow — matches the book's two-date registry; (d) document the order→operations posting policy explicitly (manual vs automatic).

IMPLEMENTATION DEPENDENCIES: owner/requirements decision on payroll accrual vs cash basis (§34–§36, TD-020 context); OQ-004 (payout rules); REVERSAL_Q1_Q7 freeze on posting semantics — new operation types only, no changes to existing legs.

---

## OQ-010. Direct vs overhead costs — what the book says

BOOK EVIDENCE: The book distinguishes sharply. ОПУ chain: «вначале формируется маржинальный доход как разница между выручкой и прямыми затратами, затем из [маржинального дохода] вычитаются накладные расходы и формируется операционная прибыль, затем из операционной прибыли вычитается амортизация и формируется чистая прибыль» (quoted verbatim: «из прямых затрат вычитаются накладные расходы» — the standard reading is margin − overhead). Overhead is modeled separately: «Моделирование накладных расходов мы начнем с расчетов с персоналом, поскольку для технологических компаний это одна из основных статей расходов» (ch on budget model: «модель накладных расходов» as its own sheet); «2002 «Прочие прямые затраты»» shows direct costs have their own article series; «Налоги – особая категория бюджетной модели. С одной стороны, налоги можно отнести к накладным расходам».

CURRENT ERP: Only Direct Cost is implemented — `v_order_costing` (database/schema.sql:2320, §37/§76): Material + Piecework Labor; Gross Profit = Revenue − Direct Cost. Overhead allocation is explicitly DEFERRED: OQ-010 («Распределение общих расходов (аренда, коммунальные, реклама) между ИП и между заказами — Аллокация накладных — DEFERRED»), DEF-002 in REQUIREMENTS_MATRIX.md («Правило распределения неизвестно (OQ-010); §37 требует только Direct Cost»).

GAP: No overhead allocation rule and no full-cost P&L; margin chain stops at gross profit per order. The book's pattern (separate overhead model → subtract from margin → operating profit) has no counterpart.

SEVERITY: P2 (matches existing DEF-002; owner decision required — allocation base unknown)

PROPOSED REQUIREMENT: Do not invent the allocation rule. When OQ-010 is answered: implement an overhead allocation model as a separate layer (book pattern: overhead model separate from direct-cost model), with the allocation base stored as configuration (not hardcoded), producing full cost per order and operating profit in P&L. Until then keep Direct Cost only.

IMPLEMENTATION DEPENDENCIES: OQ-010 owner answer (allocation base: revenue share, labor hours, etc.); TD-025 metric definitions; area 12 budget model would naturally host the overhead budget.

---

## Cross-cutting note

Areas 12–14 form one dependency chain (ЦФО → budgets → overrun control → plan-fact), all currently absent and none required by FINANCE-OPS-001 — they are requirement candidates, not defects. Area 15 is mostly satisfied by design (single registry + transfer exclusion); area 16's payroll-accrual gap is the one item that may contradict existing P&L semantics and needs a requirements review before any code change. None of this touches the frozen areas (REVERSAL_Q1_Q7, TD-020, KASPI_OQ_003, REPORT_8_DEFINITION).

Next step: owner decides whether budgeting/ЦФО/overhead-allocation enter scope (new requirement candidates), and confirms payroll posting basis (accrual vs cash) for area 16.
