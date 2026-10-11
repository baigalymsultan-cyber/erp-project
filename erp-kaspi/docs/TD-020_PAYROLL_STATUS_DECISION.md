# TD-020 — PAYROLL PERIOD/EMPLOYEE PAYMENT STATUS DECISION MEMO

**Дата исследования:** 2026-10-03
**Статус:** OPEN — требуется бизнес-решение владельца (P1, BLOCKED)
**Связанные записи:** TD-020, TD-021, `docs/TECHNICAL_DEBT.md:166-177`
**Принцип:** код не меняется до решения владельца; исследование не выдумывает семантику.

---

## 1. ВОПРОС

Какова правильная семантика `payroll_periods.status` относительно статуса оплаты отдельных сотрудников:

- (A) **производный статус** — период считается «полностью оплаченным» только когда ВСЕ `payroll_summaries` периода в `PAID` (статус периода вычисляется, а не выставляется как побочный эффект);
- (B) **явное закрытие** — отдельный владелец-действие `close_payroll_period()` переводит период в `CLOSED` после проверки, что все summaries оплачены; статус `PAID` у периода вообще убирается;
- (C) **периоды на сотрудника** — отказываемся от общего периода: каждый `payroll_summary` живёт в собственном периоде (один сотрудник = один период).

Без ответа владельца нельзя менять `pay_payroll_summary()`, семантику `PAID`/`CLOSED` и логику `calculate_payroll()`.

---

## 2. ТЕКУЩЕЕ ПОВЕДЕНИЕ (факты, проверено чтением кода)

**Сервис** — `app/services/__init__.py`:

1. `calculate_payroll()` (стр. ~250): требует `period.status NOT IN ("CLOSED", "PAID")`, иначе `ValueError`. После расчёта ставит период в `PROCESSING`.
2. `pay_payroll_summary()` (стр. ~473-475): после оплаты ОДНОГО summary ставит `summary.status = "PAID"` **и** `period.status = "PAID"`, `period.closed_at = now()`.
3. Итог: период с N сотрудниками после оплаты сотрудника №1 становится `PAID` → `calculate_payroll()` для сотрудников 2..N отказывает. Закреплено тестом `tests/test_payroll_flow.py:191` (период `PAID` после 1 из 2).
4. `payroll_periods.status` CHECK (`database/schema.sql:892`): `('OPEN','PROCESSING','CLOSED','PAID')`. `CLOSED` кодом **никогда не выставляется** (только в guard `calculate_payroll()`); `CANCELLED` для summary — мёртвое значение (см. TD-021).

**Схема:** `payroll_summaries` — по одной строке на (employee, period), свой `status` (`DRAFT`/`APPROVED`/`PAID`/...). `payroll_payments` — записи фактических выплат с защитой от двойной оплаты (`IntegrityError` → `ValueError`, §57). Оплата в системе — событие на уровне **сотрудника**, не периода.

---

## 3. PRODUCTION PATTERNS (как это устроено в реальных системах)

Общий вывод по всем пяти системам: **ни одна не выставляет периодный «PAID» как побочный эффект оплаты одного сотрудника.** Статусы уровня сотрудника/сводной ведомости и статусы уровня периода — это два независимых измерения: первое фиксирует деньги, второе — стадию рабочего процесса.

### 3.1 Odoo Payroll (официальная документация, Odoo 19.0)

- Payslip (расчётный листок сотрудника): статусы **Draft → Waiting → Done → Paid**. `Paid` выставляется per-slip: либо кнопкой `Pay`/`Mark as paid` на листке после того, как проводка проведена, либо через `Register Payment` (аналог оплаты счетов, с банковской сверкой).
- Pay run (пакет, аналог нашего `payroll_period`): статусы **Ready → Done → Paid**.
  - `Create Draft Entry` переводит пакет `Ready → Done` (все листки «обработаны»).
  - `Mark as Paid` — **явное действие пользователя** на пакете, которое постит платёжные проводки. Не автоматическое, не «первый оплаченный листок».
  - Если хотя бы один листок пакета имеет статус `Paid`, пакет **не может вернуться** в `Ready` (защита от отката после начала выплат).
- Источники:
  - https://www.odoo.com/documentation/19.0/applications/hr/payroll/pay_runs.html («For a pay run to be processed, it must have a status of Ready… After this occurs, the pay run status changes to Done… It is important to note, that if any payslips in the pay run have a status of Paid, the pay run **cannot** revert to a status of Ready»)
  - https://www.odoo.com/documentation/19.0/fr/applications/finance/accounting/payments.html (частичные платежи: `Keep open` — счёт остаётся открытым с баннером `Partial`; `Mark as fully paid` — списание разницы отдельной проводкой)
- **Паттерн Odoo:** деньги — per-slip; пакетный `Paid` — явная консолидирующая отметка пользователя, подразумевающая, что все листки оплачены; откат блокируется, если начались выплаты.

### 3.2 SAP HCM Payroll

- Payroll Control Record (транзакция PA03) — статусы рабочего процесса для **payroll area** (группы сотрудников, не денег):
  **Released for Payroll → Start Payroll → Released for Correction → Exit Payroll**.
- Переход к следующему периоду происходит **только явным действием**: «Exit Payroll» закрывает период, затем следующий «Release for Payroll» инкрементирует период. Автоматического «period paid» не существует.
- Статус оплаты — на уровне сотрудника: Infotype 0003 (Payroll Status) хранит, до какого периода сотрудник учтён; отбракованные в прогоне сотрудники — в Matchcode W. Оплата (банковский файл / DME) — отдельный пост-расчётный процесс.
- Корректировки после закрытия: off-cycle payroll (транзакция `PUOC_XX`) — доплата/возврат без открытия закрытого периода. Ошибочная выплата не «портит» период, а корректируется отдельным прогоном.
- Источники:
  - http://www.guru99.com/all-about-payroll-control-record.html (стадии control record; «only Exit closes the period and advances the control record to the next cycle»)
  - https://community.sap.com/t5/enterprise-resource-planning-q-a/what-is-the-purpose-of-pa03-payroll-control-record-in-sap-hr/qaq-p/3751437
  - https://help.sap.com/doc/92877cc12a9c4684b0c8179ce119b980/2405/en-US/Payroll_and_Post_Payroll_Processing_Test_Script_10D.pdf (Exit payroll: «The payroll period has now been closed and no further processing can occur. The payroll period is incremented in the payroll control record»)
- **Паттерн SAP:** период закрывается только явным действием оператора; «оплаченость» не является состоянием периода вообще.

### 3.3 Oracle Fusion Cloud Payroll

- Payroll flow (шаблон цикла): **Calculate Payroll → Prepayments → Archive Periodic Payroll Results → Make EFTs / Generate Check Payments → Generate Payslips → Verify Payments**. Статусы — на уровне «процесс × человек»: `Complete`, `In Error`, `Mark for Retry`, `Not Started`.
- Платёжные статусы — на уровне метода выплаты сотрудника: один платёжный метод может быть `complete/paid`, другой — `incomplete/unpaid` для того же человека. Единого флага «период оплачен» нет.
- Ошибочные выплаты: действие **Void a Payment** ставит статус `Void` и **автоматически перевыпускает** выплату в следующем платёжном прогоне (тот же тип/метод/дата). Возврат не откатывает статус периода.
- Источники:
  - https://docs.oracle.com/en/cloud/saas/human-resources/faaus/administering-payroll-for-the-united-states.pdf (Corrective Actions for Payments: «Void a payment simply updates the payment status of the record to Void… the application reissues it automatically the next time you run the payments process»)
  - https://docs.oracle.com/cd/G38197_01/trans/G34674-01/administering-global-payroll.pdf (Payroll Activity Center: pending results — «payroll calculations that are missing a prepayment», «prepayment results that are pending payments»)
- **Паттерн Oracle:** статусы гранулярны до уровня «сотрудник × метод выплаты»; корректировки — отдельными действиями, период не «ломается».

### 3.4 Microsoft Dynamics 365 Finance & Operations (US Payroll)

- Pay cycle / pay periods — это **просто даты** (сгенерированные периоды с датами выплат). Статуса «paid» у периода нет; обработка идёт только по существующим периодам.
- Цепочка на уровне сотрудника: **Earning Statement** (Generated → Released) → **Pay Statement** (Calculated) → Submit for Payment → payment journal (Payroll Disbursement Journal) → Generate Payment → пост проводки → **Issued Pay Statements**.
- Payment status — поле **earning statement**: становится `Processed` после релиза; отмена после выпуска выплаты — через **Reversing Pay Statement** (сторнирующий документ), а не откат статуса.
- Источники:
  - https://learn.microsoft.com/en-us/dynamics365/fin-ops-core/fin-ops/hr/localizations/usa/noam-usa-pay-cycle-pay-period-tasks-sample (pay periods = даты; «Pay can be processed only for pay periods that are in the system»)
  - http://sandeepchaudhuryd365.com/tag/pay-statements/ («the Payment status changes to Processed after releasing the earning statement… Submit it for Payment… automatically creates the financial journal»)
  - https://ellipsesolutions.com/dynamics-365-payroll-payment-cancellation-made-painless/ (Reverse: «A Reversing Pay Statement will be created to offset the original Pay Statement»)
- **Паттерн D365:** оплата — событие уровня pay statement сотрудника; периода как «оплаченного» объекта нет.

### 3.5 1C:ЗУП 3.1 (релевантно для рынка KZ)

- Расчёт: документ «Начисление зарплаты и взносов» (по месяцу, по всем сотрудникам).
- Выплата: **ведомости** («Выплаты — Все ведомости»): в одной ведомости — строки по сотрудникам, каждая строка помечается выплаченной отдельно. Взаиморасчёты ведутся **накопительно по сотруднику**, месяц как объект «оплачен/не оплачен» не существует.
- Источник: https://buhexpert8.ru/wp-content/uploads/2022/12/Pamyatka-RASCHET-ZARPLATY-v-ZUP-3-2023.pdf (цепочка: Начисление → Ведомости → Отражение в бухучёте; выплата — построчно по ведомости).
- **Паттерн 1C:** «оплачено» — свойство строки выплаты на сотрудника, а не месяца.

---

## 4. ОТВЕТЫ НА RESEARCH QUESTIONS

| Вопрос | Ответ из production-практик |
|---|---|
| Как моделируется статус периода vs статус сотрудника? | Два независимых измерения везде: per-employee «деньги» (payslip/summary/statement: Draft → Paid; per payment method) и period/record-level «стадия процесса» (Ready/Done, Release/Exit). Ни одна система не смешивает их в один флаг. |
| Когда период считается fully paid / закрытым? | **Явным действием оператора** (SAP `Exit Payroll`, Odoo `Mark as Paid` на пакете) либо дата периода просто проходит. Нигде — автоматическим флагом после первой выплаты. |
| Частичные выплаты (часть сотрудников оплачена)? | Штатный сценарий везде: Odoo — `Register Payment` per-slip + частичные платежи (`Partial` banner); Oracle — статусы per payment method; D365 — per pay statement. Остальные сотрудники продолжают обрабатываться, период не блокируется. |
| Multi-employee: статус периода выводится из выплат или независим? | Независим. SAP: control record вообще не знает про выплаты. Odoo: пакетный Paid — явное действие, подразумевающее все листки paid, но выплаты идут per-slip и не блокируют друг друга. |
| Failed/returned payments: влияние на статус периода? | Нулевое влияние на период. Oracle: `Void a Payment` → автовыпуск в следующем прогоне. D365: reversing pay statement. SAP: off-cycle после exit. Корректировка — отдельным объектом/прогоном, период не «портится» и не откатывается. |
| Правильная семантика для erp-kaspi? | `period.status` должен описывать **стадию расчёта** (OPEN/PROCESSING/CLOSED), а не деньги. Деньги — только `payroll_summaries.status` + `payroll_payments`. Текущий `period.status = "PAID"` после одной выплаты — антипаттерн, не встречающийся ни в одной исследованной системе. |

---

## 5. ОПЦИИ

### Вариант A — Производный статус периода (recommended by evidence)

`pay_payroll_summary()` выставляет `PAID` **только** на `payroll_summary`. `period.status = "PAID"` выставляется только когда **все** summaries периода в `PAID` (проверка `NOT EXISTS (summaries WHERE status != 'PAID')` в той же транзакции). `calculate_payroll()` продолжает отказывать `PAID`-периодам — но теперь это корректно, потому что все сотрудники действительно оплачены. Отдельный сотрудник всегда может быть досчитан/доплачен, пока есть неоплаченные summaries.

- **Плюсы:** минимальный дифф; полностью снимает TD-020; соответствует Odoo-паттерну «пакет paid ⟺ все листки paid»; сохраняется инвариант «PAID-период = деньги розданы полностью»; idempotency и §57-локи не трогаем.
- **Минусы:** статус периода остаётся смешанным измерением (деньги + процесс); нужен явный ответ владельца, что делать с `CLOSED` (TD-021 остаётся открытым); при возврате выплаты (void) статус периода не откатывается автоматически — нужен отдельный процесс.
- **Влияние на архитектуру:** +1 проверка в `pay_payroll_summary()`; тест `test_payroll_flow.py:191` придётся переписать (он кодирует багованное поведение — допустимо, т.к. это тест на баг, а не на требование).

### Вариант B — Явное закрытие, убрать PAID у периода

`payroll_periods.status` сводится к чистому процессу: `OPEN → PROCESSING → CLOSED`. Новое действие `close_payroll_period(period_id)`, которое проверяет «все summaries PAID» и только тогда ставит `CLOSED` + `closed_at`. `pay_payroll_summary()` вообще не трогает `period.status`. CHECK-ограничение меняется (`PAID` убирается из списка — миграция). Это закрывает и TD-021 (`CLOSED` становится достижимым и осмысленным) и приближается к SAP-паттерну (явный Exit).

- **Плюсы:** самое чистое разделение измерений (деньги vs процесс); закрывает TD-020 и TD-021 одним решением; прямой аналог SAP `Exit Payroll` / Odoo `Mark as Paid` на пакете.
- **Минусы:** больше изменений (новый endpoint/сервис, миграция CHECK); ломает любые отчёты/UI, опирающиеся на `period.status='PAID'` (проверить `frontend/`, `reports.py`); владельцу нужно подтвердить, что `PAID` как статус периода не нужен в отчётности.
- **Влияние на архитектуру:** миграция + новый сервис-метод + UI-кнопка закрытия; audit-триггер (A-1) уже покрывает `payroll_periods`.

### Вариант C — Периоды на сотрудника

Отказ от общего периода: каждый сотрудник получает собственный `payroll_period` (или `payroll_summary` становится самодостаточной единицей жизненного цикла). Период «оплачен» ⟺ его единственный summary оплачен — TD-020 исчезает по построению.

- **Плюсы:** TD-020 невозможен архитектурно.
- **Минусы:** ломает саму идею «расчётного периода» (Master Prompt §34 — периоды месячные, общие); дублирование периодов × N сотрудников; сводная отчётность по периоду усложняется; противоречит всем пяти исследованным системам, где период/пакет — общий контейнер. **Не рекомендуется.**
- **Влияние на архитектуру:** перепроектирование payroll-модуля; затрагивает Master Prompt §34 — почти наверняка требует пересогласования требований.

---

## 6. ЧТО СИСТЕМА МОЖЕТ РЕШИТЬ САМА vs ЧТО ТРЕБУЕТ OWNER

**Система может решить сама (объективно следует из evidence):**
- Текущее поведение — баг: ни одна production-система не ставит «период оплачен» после одной выплаты; это блокирует сотрудников 2..N и противоречит §34. Исправление направления (не ставить `PAID` на период за одну выплату) — инженерное решение, не бизнес-семантика.
- `payroll_summaries.status` + `payroll_payments` — правильное место для «денег» (подтверждено всеми пятью системами).

**Требует решения OWNER (бизнес-семантика, не выдумывать):**
1. **Вариант A vs B vs C** — что означает «период полностью оплачен» в их учётной практике: достаточно ли «все summaries PAID» (A) или нужен отдельный акт закрытия с ответственным (B)?
2. **Судьба статуса `PAID` у периода**: оставить (A) или убрать из CHECK-ограничения (B)? Есть ли внешняя отчётность/ИП-учёт, где «PAID-период» — значимый срез?
3. **Судьба `CLOSED`** (TD-021): мёртвое значение сейчас. При варианте B оно оживает как «закрыт актом». При варианте A — удалить из CHECK или оставить на будущее?
4. **Возвраты/неуспешные выплаты**: если выплата сотруднику возвращена банком после того, как период стал PAID/CLOSED — период откатывается, остаётся закрытым с корректировкой (SAP off-cycle паттерн), или это новая выплата в текущем периоде? Влияет на то, нужен ли переход PAID → PROCESSING обратно.
5. **Частичная выплата одному сотруднику** (аванс/доля): допустима ли в их практике? Если да — понадобится статус `PARTIALLY_PAID` на summary (паттерн Odoo `Partial`), сейчас его нет.

---

## 7. РЕКОМЕНДАЦИЯ ИССЛЕДОВАНИЯ (не решение владельца)

По совокупности evidence (5/5 систем, включая официальную документацию Odoo и SAP) рекомендуемый вектор — **A как немедленный фикс бага + B как целевая модель**:

1. **Сейчас (инженерно, без бизнес-семантики):** убрать выставление `period.status="PAID"` + `closed_at` из `pay_payroll_summary()` за одну выплату. Это чистый багфикс: поведение «оплата сотрудника №1 блокирует сотрудников 2..N» не защищается никаким требованием Master Prompt §34.
2. **После ответа владельца:** либо A (производный PAID при всех summaries PAID), либо B (явное `close_payroll_period()` + оживление `CLOSED`).
3. **Не делать:** вариант C (противоречит §34 и всем исследованным системам); выдумывать `PARTIALLY_PAID`, правила возвратов и судьбу `CLOSED` до ответов на вопросы 2–5 выше.

До решения владельца: **код не трогать** (BLOCKED), тест `test_payroll_flow.py:191` оставить как есть (он документирует текущее поведение как known-bug evidence).

---

## 8. ИСТОЧНИКИ (все проверены 2026-10-03, цитаты — дословно из документов)

| # | Система | Документ | URL | Дата актуальности |
|---|---|---|---|---|
| 1 | Odoo | Pay runs (офиц. документация, v19.0) | https://www.odoo.com/documentation/19.0/applications/hr/payroll/pay_runs.html | документация текущей версии (проверена 2026-10-03) |
| 2 | Odoo | Payments: partial payments (офиц. документация, v16.0) | https://www.odoo.com/documentation/16.0/fr/applications/finance/accounting/payments.html | документация v16 (проверена 2026-10-03) |
| 3 | SAP | Payroll Control Record (guru99, по материалам SAP) | http://www.guru99.com/all-about-payroll-control-record.html | обновлено ~88 дней назад (проверена 2026-10-03) |
| 4 | SAP | PA03 purpose (SAP Community) | https://community.sap.com/t5/enterprise-resource-planning-q-a/what-is-the-purpose-of-pa03-payroll-control-record-in-sap-hr/qaq-p/3751437 | архив сообщества (проверена 2026-10-03) |
| 5 | SAP | Payroll and Post Payroll Processing Test Script (help.sap.com, PDF) | https://help.sap.com/doc/92877cc12a9c4684b0c8179ce119b980/2405/en-US/Payroll_and_Post_Payroll_Processing_Test_Script_10D.pdf | релиз 2405 (проверена 2026-10-03) |
| 6 | Oracle | Administering Payroll for the United States (docs.oracle.com, PDF) | https://docs.oracle.com/en/cloud/saas/human-resources/faaus/administering-payroll-for-the-united-states.pdf | Fusion Cloud Payroll (проверена 2026-10-03) |
| 7 | Oracle | Administering Global Payroll (docs.oracle.com, PDF) | https://docs.oracle.com/cd/G38197_01/trans/G34674-01/administering-global-payroll.pdf | Global Payroll (проверена 2026-10-03) |
| 8 | MS Dynamics 365 | Set up pay cycles and pay periods (learn.microsoft.com) | https://learn.microsoft.com/en-us/dynamics365/fin-ops-core/fin-ops/hr/localizations/usa/noam-usa-pay-cycle-pay-period-tasks-sample | текущая документация (проверена 2026-10-03) |
| 9 | MS Dynamics 365 | Pay statements processing (независимый блог по D365) | http://sandeepchaudhuryd365.com/tag/pay-statements/ | (проверена 2026-10-03) |
| 10 | MS Dynamics 365 | Payroll payment cancellation (Ellipse Solutions) | https://ellipsesolutions.com/dynamics-365-payroll-payment-cancellation-made-painless/ | (проверена 2026-10-03) |
| 11 | 1C:ЗУП 3.1 | Шпаргалка по расчёту зарплаты (buhexpert8.ru, PDF) | https://buhexpert8.ru/wp-content/uploads/2022/12/Pamyatka-RASCHET-ZARPLATY-v-ZUP-3-2023.pdf | редакция 2023 (проверена 2026-10-03) |

**Ограничение исследования:** Oracle/SAP/Dynamics/1C — по публичной документации и вторичным источникам (guru99, SAP Community, блоги практиков); внутренняя логика вендоров закрыта. Официальные первоисточники — Odoo docs, help.sap.com, docs.oracle.com, learn.microsoft.com. Ни один источник не описывает паттерн «период = PAID после первой выплаты» — что само по себе является finding'ом.
