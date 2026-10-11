# REVERSAL RESEARCH — Production patterns for posted-transaction reversal

**Дата:** 2026-10-03
**Статус:** research only. Не код, не решение. Блокеры REVERSAL_Q1_Q7 / TD-020 / KASPI_OQ_003 / REPORT_8_DEFINITION этим документом не закрываются — это внешний референс для будущего ADR-016.
**Системы:** Oracle Financials (EBS GL, Fusion GL, AP), SAP (ECC FI, S/4HANA), Microsoft Dynamics 365 (Finance, Business Central), Odoo (Accounting 15–18), NetSuite, QuickBooks Online, Xero.
**Метод:** веб-поиск по официальным источникам (docs.oracle.com, learn.microsoft.com, odoo.com/documentation, docs.oracle.com/en/cloud/saas/netsuite, quickbooks.intuit.com/learn-support, central.xero.com) + SAP Community/операционные гайды там, где help.sap.com не отдавал прямых страниц. Каждая строка снабжена ссылкой. То, что не подтверждено источником, помечено **not confirmed**.

**Терминологическая заметка:** в этом документе Q1–Q7 — это семь исследовательских вопросов (см. ниже). Q1–Q7 из `docs/REVERSAL_DECISION_MEMO.md` — это семь вопросов владельцу. Маппинг «исследование → вопросы владельцу» дан в разделе 9.

---

## Короткий вывод (TL;DR)

1. **Ни одна из семи исследованных систем не делает reversal через status flip на posted-строке.** Compensating journal — консенсус: новая сторнирующая проводка, оригинал неизменен. Status flip используется только как *аннотация на документе* (флаг Canceled/Voided/Reversed), а не как финансовый механизм.
2. **Оригинал всегда получает метку/ссылку на реверс** (bidirectional link): STBLG/XREVERSAL (SAP), Reversal Status (Oracle), Canceled=Yes (BC), reversed_entry_id (Odoo), Void Of/Voided On (NetSuite).
3. **Реверс реверса запрещён** везде, кроме D365 Finance (там явно разрешён как цепочка компенсаций). Типовой выход из ошибочного реверса — третья ручная компенсирующая проводка.
4. **Дата реверса никогда не попадает в закрытый период.** Два лагеря: «дата оригинала, если период открыт, иначе текущий открытый период» (SAP, D365 Finance) vs «текущая дата с автосдвигом за lock date» (Odoo, QuickBooks). Консенсус: posting-lock периода — жёсткий гейт.
5. **Correction / reversal / void / refund** — четыре разных операции во всех системах: правка до постинга → компенсация после постинга → бизнес-аннулирование с записью → возврат денег как отдельный документ.
6. **Защита исторического P&L** = закрытие периодов (period close / lock date / Allow Posting windows), а не флаг на документе.
7. **Идемпотентность** везде state-based («already voided», «reverse only once», in-flight guard); idempotency-key уровня API ни у кого не документирован — принятый в erp-kaspi подход (C-1) строже индустрии.

---

## Q1. Compensating journal vs status-flip — как production-системы разворачивают POSTED-транзакцию

### Oracle Financials (EBS GL / Fusion GL)

- Reverse = **новые компенсирующие проводки** через операцию «Reverse». Исходная posted-проводка **никогда не изменяется**.
- Реверсирующий batch сначала создаётся как UNPOSTED, потом постится отдельно. Имя: `Reverses [Original Journal Entry Name] [Date] [Time]`.
- Два метода: **Switch Dr/Cr** (дебет↔кредит меняются местами — для аккруалов) и **Change Sign** (та же сторона, отрицательная сумма — для исправления ошибок ввода). По умолчанию в Fusion: Switch Dr/Cr.
- Исходник получает view-only статус **Reversal Status** = `Reversed` (было `Not reversed`) — это блокирует повторный реверс.
- Источники:
  - https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/revers01.htm (Generating Reversing Journal Batches)
  - https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/reverse.htm (Defining Reverse Journal Entries)
  - https://docs.oracle.com/cd/G49759_01/trans/G46389-01/using-general-ledger.pdf (Fusion: «When does a journal become reversible?» — «posted and not yet reversed»)

### SAP (ECC FI / S/4HANA)

- FB08 (индивидуальный реверс) / F.80 (массовый) / FBRA (для клирингованных) постят **новый reversal-документ** с противоположными debit/credit. Оригинал неизменен навсегда.
- Оригинал штампуется: BKPF-**STBLG** (номер reversal-документа), BKPF-**XREVERSAL**=`1` («reversed»), у самого reversal-документа XREVERSAL=`2` («reversal document»); на уровне строк BSEG-XRAGL. Связь двусторонняя.
- **Negative postings (Storno)**: в зависимости от reversal reason система постит либо «нормальный» реверс (противоположная сторона — увеличивает обороты счёта), либо negative posting (та же сторона, отрицательная сумма — обороты счёта *уменьшаются*, нетто ноль). Это конфигурируется флагом «Negative postings allowed» на причине реверса.
- Предусловия реверса (иначе отказ): нет cleared items; только vendor/customer/G-L строки; документ не является reversal-документом и ещё не реверсирован; тип документа допускает реверс.
- Источники (официальный help.sap.com не отдавал прямых страниц; факты подтверждены операционными гайдами и SAP Community):
  - https://www.guru99.com/how-to-perform-document-reversal.html
  - https://community.sap.com/t5/application-development-discussions/payment-document-reversed/td-p/6586971 (XREVERSAL 1/2, STBLG/STJAH)
  - https://community.sap.com/t5/enterprise-resource-planning-q-a/how-to-display-all-reversal-document/qaq-p/2950003
  - https://cg.sc.gov/sites/cg/files/Documents/Guidance%20and%20Forms%20for%20State%20Agencies/CGs%20Accounting%20Policies%20and%20Procedures/Accounting%20Guidance/How%20to%20Record%20Document%20Reversals%20Using%20SCEIS%20Transaction%20Code%20FB08.pdf

### Microsoft Dynamics 365

- **D365 Finance**: Reverse journal posting создаёт **reversal journal, зеркалящий оригинал**, связанный с исходным journal header для трассируемости. Источники: https://learn.microsoft.com/en-us/dynamics365/finance/general-ledger/reverse-journal-posting, https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions
- **Business Central**: гибрид. Для журнальных проводок — компенсирующая запись с противоположным знаком. Для posted sales invoice — **Cancel** = auto-create+post corrective **credit memo** (новый компенсирующий документ, применённый ко всем G/L и inventory-записям оригинала) **+** установка флага **Canceled=Yes** на оригинале (status-flip как аннотация, не как финансовый механизм). Для production orders — компенсирующие item journal lines с противоположным знаком.
- Источники: https://learn.microsoft.com/en-us/training/modules/correct-entries/4-reverse-journal-postings, https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice, https://learn.microsoft.com/pt-pt/dynamics365/business-central/finance-how-reverse-journal-posting

### Odoo

- **Reverse Entry** wizard создаёт **новую проводку-зеркало** (debit↔credit swapped), Reference = «Reversal of: <original>», ссылка на оригинал (Other Info; поле `reversed_entry_id` в кастомизациях/автоматизации).
- **Storno accounting** (опциональная настройка, обязательна в ряде EU-локализаций): копия оригинала с **отрицательными суммами**.
- **Reset to Draft** — отдельный путь исправления posted-документа (сброс в draft с авто-разреконсиляцией), блокируется для hash-locked журналов («Lock Posted Entries with Hash») и документов, ушедших в налоговую.
- Источники: https://hibou.io/docs/accounting-3/journal-entries-90/ver/14-0-21, https://www.odoo.com/documentation/18.0/applications/finance/accounting/reporting/year_end.html, https://www.cybrosys.com/blog/how-to-manage-reversal-of-the-journal-entries-in-odoo-17-storno-accounting

### NetSuite

- Конфигурируемо: default **Void** = zero-out (сумма и строки → 0, запись остаётся) **или** с preference «Void Transactions Using Reversing Journals» — Void создаёт **reversing journal** с противоположными суммами; поле **Void Of** связывает с оригиналом, у оригинала статус **Voided** + дата **Voided On** (гибрид: компенсация + status-flip-аннотация).
- Источники: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N563543.html, https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N1469460.html

### QuickBooks Online

- Три раздельных операции: **Void** (zero-out, дата оригинала сохраняется, запись остаётся), **Delete** (удаление с неизбежным следом в Audit Log), **Reverse** (новая компенсирующая journal entry, часто с суффиксом «R» в номере).
- Источники: https://quickbooks.intuit.com/learn-support/en-ca/help-article/list-management/void-delete-transactions-quickbooks-online/L5sZV8GYh_CA_en_CA, https://plugin-qbo.intuit.com/qbo-ush/helpcontent/production/124/en_GB/QBO_Plus/Content/Topics/account/help_account_registers_delete.htm

### Xero

- Чистейший status-flip на уровне документа: AUTHORISED → **VOIDED** (удаление authorised-документов запрещено; VOIDED остаётся в списках, нумерация не рвётся). Компенсирующих journal entries нет вообще; финансовый эффект нейтрализуется **credit note** (отдельный документ).
- Источник: https://central.xero.com/0/article/Delete-or-void-a-sales-invoice

### Вывод по Q1 + опции для erp-kaspi

| Опция | Соответствие индустрии | Pros | Cons |
|---|---|---|---|
| **A. Compensating journal** (новая операция, оригинал неизменен) | Oracle, SAP, D365 Finance, BC (журналы), Odoo, NetSuite (режим), QBO (Reverse), Xero (credit note) — **консенсус** | Полный аудит-трейл; оригинал как финансовый факт; возможен частичный реверс; совместимо с trigger-защитой POSTED-строк | Нужен тип компенсирующей записи (см. memo Q2, CHECK `chk_operation_types_dir_kind`); двусторонняя ссылка; решение по P&L-отображению (валовые обороты вырастут) |
| **B. Status flip** (POSTED→REVERSED на той же строке) | **Нет прецедента** как финансового механизма. Status-flip существует только как *аннотация документа* (Canceled/Voided/Reversed-флаг) поверх компенсации | Ноль изменений кода | Не audit-proof: нет второго документа; нет частичного реверса; противоречит Master Prompt §1 и ADR-007; trigger `trg_operations_protect_posted_ledger` сейчас разрешает этот переход только как временную совместимость |
| **C. Гибрид** (флаг на оригинале + компенсация) | **D365 Business Central** (Cancel: credit memo + Canceled=Yes), **NetSuite** (reversing journal + Voided), SAP (XREVERSAL-аннотация + reversal-документ) — **второй консенсус** | Лучшее из обоих: финансы — компенсацией, читаемость — флагом | Чуть больше схемы (флаг + ссылка) |

**Вывод исследования:** индустрия голосует за **A или C**; чистый B не используется никем из семи систем как способ отмены posted-транзакции. Минимальный C = A + булев/статусный маркер «reversed» на оригинале + двусторонняя ссылка — это ровно то, что делают SAP (XREVERSAL), Oracle (Reversal Status) и BC (Canceled).

### Impact на архитектуру erp-kaspi (если владелец выберет A/C)

- `operations`: оригинал **не мутирует** (уже гарантировано `trg_operations_protect_posted_ledger`); реверс = новая строка `operations` со статусом POSTED.
- **Двусторонняя связь обязательна.** `related_operation_id` сейчас занят transfer-парингом (memo §3.1, TD-018 запрещает переиспользовать) → нужна отдельная колонка (напр. `reversal_of_id`) или отдельная таблица связей. SAP-аналог: STBLG + XREVERSAL (1=reversed / 2=reversal).
- `operation_types`: CHECK `chk_operation_types_dir_kind` (INCOME→IN, EXPENSE→OUT, TRANSFER→IN/OUT) блокирует наивную компенсацию; потребуется решение по типу (новый kind/code vs mapping) — **бухгалтерское решение владельца**, индустрия не даёт готового ответа для малого бизнеса (SAP решает reversal reason'ами с флагом negative posting; Odoo — Storno).
- `v_ip_financial_summary` / `pnl()`: пара POSTED+компенсация даст нетто-ноль при росте валовых оборотов → решение «свёрнуто vs развёрнуто» (memo Q7).
- Отдельное поле `reason` вместо перезаписи `comment` (TD-019): у SAP это STGRD (reversal reason), у D365/Odoo — reason в reversal-документе.

---

## Q2. Reversal date — same period vs next period? Что контролирует дату?

### Паттерны

- **Oracle**: реверс всегда идёт в явный **reversal period** — параметр операции: Same Period / Next Period / Next Non-Adjusting Period (+ Next Day/First Day/Last Day для average balance ledgers). Жёсткое правило: reversal period обязан быть **open или future-enterable** («it may not be reversible… if the reversal period or effective date is closed»). В закрытый период реверс невозможен. Источники: https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/revers01.htm, https://docs.oracle.com/cd/G49759_01/trans/G46389-01/using-general-ledger.pdf
- **SAP**: default = **дата оригинала** (поле Posting Date опционально; пустое = дата исходного документа). Но период оригинала должен быть **открыт** (OB52). Если закрыт — обязателен reversal reason, разрешающий alternative posting date (02/04), и реверс постится в **текущий открытый период**. Постинг в закрытый период отклоняется ошибкой (напр. EP443). Источники: https://www.guru99.com/how-to-perform-document-reversal.html, https://community.sap.com/t5/enterprise-resource-planning-q-a/reversal-reason/qaq-p/5787058
- **D365 Finance**: диалог предлагает «Yes = сохранить даты оригинала / No = ввести новую дату». «If the reversing date is in a fiscal period that isn't open, the transaction can't be reversed»; можно пере-датировать реверс в открытый период. Источник: https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions
- **Business Central**: reversing entry обязана иметь **тот же document number и posting date, что оригинал**. Плюс окна Allow Posting From/To (компания/пользователь/шаблон журнала) блокируют постинг вне интервала; есть per-user override для закрытых периодов. Источники: https://learn.microsoft.com/pt-pt/dynamics365/business-central/finance-how-reverse-journal-posting, https://learn.microsoft.com/is-is/dynamics365/business-central/localfunctionality/belgium/how-to-limit-the-posting-period
- **Odoo**: wizard: Specific (default = **текущая дата**) или Journal Entry Date (дата оригинала). Lock date блокирует постинг на/до lock date — система **автоматически сдвигает accounting date на день после lock date**. Источники: https://hibou.io/docs/accounting-3/canceling-journal-entries-80, https://www.odoo.com/documentation/18.0/applications/finance/accounting/reporting/year_end.html
- **NetSuite**: смысл preference «reversing journals» — разрешить void датой, **отличной от даты оригинала**; закрытые периоды не принимают void («To void a check from a closed period, you need to reopen the period first»). Источники: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N1469460.html
- **QuickBooks**: Void **сохраняет дату оригинала** (сам void не пере-датируется); closing date + опциональный пароль блокируют правки до него. Источник: https://quickbooks.intuit.com/learn-support/en-ca/help-article/list-management/void-delete-transactions-quickbooks-online/L5sZV8GYh_CA_en_CA
- **Xero**: void — смена статуса без даты; lock date блокирует void документов до неё; стандартный выход — **credit note датой после lock date**. Источник: https://central.xero.com/0/article/Delete-or-void-a-sales-invoice

### Консенсус

**Реверс никогда не попадает в закрытый/залоченный период.** Два лагеря по дефолту даты: (1) «дата оригинала, если период открыт, иначе текущий открытый период» (SAP, D365 Finance); (2) «текущая дата» (Odoo). Общий знаменатель: дата реверса = **параметр с валидацией против posting-lock**.

### Опции для erp-kaspi

| Опция | Pros | Cons |
|---|---|---|
| **A. Дата = параметр `reversal_date`, default = дата оригинала; валидация «период открыт», иначе отказ** (SAP/D365F-школа) | Предсказуемо; P&L периода оригинала корректируется в том же периоде, если он открыт | Требует сущности «учётный период» со статусами — сейчас в схеме erp-kaspi её **нет** (есть только `payroll_periods`, проверено grep по schema.sql 2026-10-03) |
| **B. Дата = текущая дата всегда** (Odoo-школа) | Не нужен period-control для старта; закрытые периоды защищены автоматически | Коррекция «уезжает» в текущий период даже при открытом исходном — может исказить месячную отчётность |
| **C. Дата = параметр, default = текущая дата; валидация «не раньше lock date»** | Компромисс; lock date — минимальная сущность (одна дата на IP/компанию), а не полная таблица периодов | Менее гибко, чем A; при открытом исходном периоде всё равно постится в текущий |

### Impact на архитектуру

- Любая опция, кроме B, требует **posting-lock**: минимум — `lock_date` (на компанию/IP), максимум — таблица `posting_periods(period, status)` (SAP OB52-школа; BC Allow Posting From/To).
- Дата компенсирующей операции должна быть её **собственным полем** (`operation_date` уже есть в модели? — проверить при реализации), а не копией даты оригинала без валидации.
- **Система может решить сама:** дефолт даты (текущая) и правило «никогда в закрытый период». **Владельцу:** нужен ли period-control вообще и какой (lock date vs периоды), допустим ли backdate в открытый прошлый период.

---

## Q3. Reversal-of-reversal — разрешён? Как?

### Паттерны

- **Oracle**: **запрещён**. Критерии Fusion «When does a journal become reversible?»: «The journal isn't a reversal journal. **Note: Reversal journals can't be reversed**» (manual и automatic). Источники: https://docs.oracle.com/cd/G49759_01/trans/G46389-01/using-general-ledger.pdf, https://docs.oracle.com/cd/E25178_01/fusionapps.1111/e20374/F484499AN19B45.htm
- **SAP**: **запрещён штатно.** FB08/FBRA отклоняют документы, которые сами являются reversal (проверка «not a reversing document or already reversed»; при попытке — «the reversal document cannot be reset»). Документированный workaround — **третий, ручной компенсирующий документ** (F-02 «post with reference» → «Generate reverse posting»), ссылающийся на reversal-документ. Цепочка A→B→C — нормальные данные, а не ошибка. Источники: https://community.sap.com/t5/enterprise-resource-planning-q-a/fbra-reversed-document-reversal-issue/qaq-p/12679926/comment-id/4129544, https://www.scribd.com/document/549103969/Sapfi-Class
- **D365 Finance**: **явно разрешён** — единственный из семи. «After the reversal is completed, **a reversed transaction can be reversed again (that is, the reversal can be reversed)**». Цепочка компенсаций с trace-ссылками. Источник: https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions
- **Business Central**: «An entry can only be reversed one time» — про reversal-записи прямо не сказано (not confirmed).
- **Odoo / NetSuite / QuickBooks / Xero**: **not confirmed** в найденных официальных документах. Xero: voided нельзя un-void (только копировать в новый документ) — void терминален.

### Опции для erp-kaspi

| Опция | Pros | Cons |
|---|---|---|
| **A. Запретить реверс реверса** (Oracle/SAP-школа) | Просто; нет цепочек; аудит читаем | Ошибочный реверс правится только ручной операцией — нужен понятный UX для этого |
| **B. Разрешить как цепочку компенсаций** (D365F-школа) | Симметрично; любой ошибочный реверс «откатывается» тем же API | Цепочки A→B→C усложняют отчётность и UI; нужен лимит/индикация глубины |
| **C. Разрешить только через ручную компенсирующую операцию** (SAP-workaround как фича) | То же, что A, но с явным инструментом вместо «руками в журнале» | По сути A + UX |

### Impact на архитектуру

- При A: guard в `reverse_operation()` — отказ, если операция сама является reversal (нужен маркер/тип reversal-операции, чтобы отличить; сейчас его нет).
- При B/C: `reversal_of_id` должен поддерживать цепочки (self-reference без ограничения глубины) + UI/отчёты должны показывать цепочку.
- **Система может решить сама:** технически цепочки поддерживаются любым вариантом ссылки. **Владельцу:** допустим ли реверс реверса как бизнес-операция (A vs B).

---

## Q4. Audit trail — как выглядит правильный аудит-трейл реверса

### Паттерны (консенсус: двусторонние ссылки + причина + who/when)

- **Oracle**: связь кодируется именем reversal batch `Reverses <name> <date> <time>`; view-only статус Reversed у оригинала; отчёты **AutoReverse Execution report** (какие журналы реверсированы, в какой период) и Posting Execution Report. Отдельной audit-таблицы реверсов нет. Источники: https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/revers01.htm, https://docs.oracle.com/applications/farel12/financialsop_gs/FAUGL/FAUGL.pdf
- **SAP**: двусторонние ссылки BKPF-STBLG/STJAH (оригинал→реверс и обратно), индикаторы XREVERSAL 1/2, **reversal reason** (STGRD, коды 01–04+) на reversal-документе, who/when в header (USNAM/CPUDT/CPUTM), document flow через модули. Аудиторы запрашивают `XREVERSAL IN (1,2)` для списка всех реверсов. Источники: https://community.sap.com/t5/application-development-discussions/payment-document-reversed/td-p/6586971, https://community.sap.com/t5/enterprise-resource-planning-q-a/how-to-display-all-reversal-document/qaq-p/2950003
- **D365 Finance**: reversal journal связан с original journal header; диалог **Reverse tracing** показывает обе транзакции с linked trace number; поле **Reversal journal ID**. Источники: https://learn.microsoft.com/en-us/dynamics365/finance/general-ledger/reverse-journal-posting, https://learn.microsoft.com/en-us/dynamics365/finance/asset-leasing/reverse-posted-lease-trans
- **Business Central**: corrective credit memo **сохраняется как аудит-трейл** («leaves the corrective posted sales credit memo for your audit trail»); флаг Canceled на оригинале; Reverse Register на G/L Register. Источник: https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice
- **Odoo**: Reference «Reversal of: INV/xxxx, <reason>» (reason из wizard → в ref реверса); ссылка на оригинал на вкладке Other Info; credit notes нумеруются R+номер инвойса; у оригинала метка «Reversed.». Источники: https://hibou.io/docs/accounting-3/journal-entries-90/ver/14-0-21
- **NetSuite**: поля **Void Of** / **Voided On** (двусторонняя связь); **System Notes** на каждой транзакции (who/when/old/new/context); Transaction Audit Trail; удалённые записи ищутся через Deleted Record search. Источники: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N1469460.html
- **Xero**: секция **History & Notes** на каждой транзакции (кто/когда/что, включая кто void'нул). Источник: https://central.xero.com/0/article/Delete-or-void-a-sales-invoice
- **QuickBooks**: Audit Log (кто/что/когда); voided-транзакции видны в отчётах с $0.00, удалённые — только в Audit Log. Источник: https://quickbooks.intuit.com/learn-support/en-sg/help-article/close-books/close-books-quickbooks-online/L59LelyPM_SG_en_SG

### Минимальный набор артефактов (выводится из консенсуса)

1. Двусторонняя ссылка оригинал↔реверс (запрос «все реверсы» — одним фильтром).
2. Маркер на обеих сторонах (reversed / reversal).
3. Причина реверса как отдельное поле на reversal-документе (не перезапись комментария оригинала — ср. TD-019).
4. Who/when на обеих записях (в erp-kaspi: существующий `audit_log` уже покрывает; нужно проверить, что reversal пишет оба события).
5. Имя/номер reversal-документа, производное от оригинала (для человеческого поиска).

### Impact на архитектуру

- Нужны: отдельная ссылка `reversal_of_id` (см. Q1 — не `related_operation_id`), маркер reversed на оригинале (статус или флаг), поле `reason` на реверсе.
- Отдельной audit-сущности не требуется — достаточно `audit_log` + ссылок (Oracle/SAP-прецедент).
- **Система может решить сама:** формат ссылок, именование, какие события писать в audit_log. **Владельцу:** ничего — это чисто техническая область, но владелец должен знать, что аудит-трейл варианта B (status flip) индустрия считает недостаточным.

---

## Q5. Correction vs reversal vs refund vs void — как системы различают

### Консенсусная таксономия (все семь систем, детали различаются)

| Операция | Когда допустима | Механизм | Примеры |
|---|---|---|---|
| **Correction** (исправление) | Документ **не запощен** (или запощен, но меняются только нефинансовые поля) | Правка in place | Oracle: «Edit unposted journals»; SAP FB02 — только описательные поля (дата постинга, суммы, счета — нельзя); Odoo: правка draft; BC: правка draft |
| **Reversal** (сторно) | **Posted**, запись ошибочна | Компенсирующая проводка/документ (см. Q1) | SAP FB08, Oracle Reverse, Odoo Reverse Entry, D365F reverse journal |
| **Void / Cancel** (аннулирование) | Документ/платёж аннулируется как бизнес-факт; запись **сохраняется** | Статус-флаг + (часто) авто-компенсация | Oracle AP: void payment → авто-сторнирование учёта, статус reset to Unpaid; **void необратим** («you cannot reverse a void on a void payment»); SAP: FCH8 void чека + реверс платёжного документа; VF11 отмена SD-инвойса; BC: Cancel → Canceled=Yes + credit memo; NetSuite: Void → Voided; QBO: Void → zero-out; Xero: VOIDED |
| **Refund** (возврат денег) | Деньги реально возвращаются контрагенту | **Отдельный платёжный документ**, никогда не реверс инвойса | SAP: outgoing payment F-53; NetSuite: Customer Refund (ошибка TRANS_ALREADY_REFUNDED — один refund на транзакцию); QBO: refund receipt; Xero: credit note + возврат |

Ключевые различения с источниками:
- **SAP**: reversal — для *ошибочных* проводок; credit/debit memo — для *легитимных бизнес-коррекций* (спор о цене, частичный возврат, возврат товара): «in all other cases they use credit/debit memo requests». Refund — отдельный платёжный документ. Источники: https://community.sap.com/t5/enterprise-resource-planning-q-a/reversing-a-fi-document-related-to-sd/qaa-p/12210652, https://www.erpgreat.com/financial/mass-void-those-wrongly-issued-checks.htm
- **Oracle**: correction — правка до постинга («Edit unposted journals to change or correct information»); reversal — после; void — отдельная терминальная операция по платежу; cancel — бизнес-событие (Invoice Canceled и др. в SLA), генерирующее сторнирующие проводки. Источники: https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/jeover.htm, https://docs.oracle.com/cd/E18727-01/doc.121/e12797.pdf, https://www.oracle.com/technetwork/fusion-apps/paycash-e22897-02-1514523.pdf
- **Odoo**: Draft → правка; Reset to Draft → исправление posted (с условиями); Reverse (credit note) — «the only option for invoices marked as in payment or paid»; Full Refund — validated credit note + авто-реконсиляция. Источник: зеркало официальных доков Odoo 17 (https://documentation.erptoancau.com/17.0/applications/finance/accounting/customer_invoices/credit_notes.html — официальный URL-паттерн: odoo.com/documentation/17.0/…; страница напрямую не открывалась, цитируется как mirror)
- **Xero**: Draft/awaiting approval → Delete; AUTHORISED → только VOIDED (удаление запрещено); Paid → сначала снять платёж (Remove & Redo), потом void, либо credit note. Источник: https://central.xero.com/0/article/Delete-or-void-a-sales-invoice

### Вывод для erp-kaspi

Индустрия держит **четыре разных кодовых пути** с разными причинами/документами. Смешивание их в один `reverse_operation()` — антипаттерн. Конкретно для erp-kaspi:
- «Возврат продажи Kaspi» (OQ-006) — это **refund/credit-memo** сценарий, а не reversal ошибочной проводки. Проектировать reversal API под возвраты — ошибка таксономии.
- Void (аннулирование платежа/документа с сохранением записи) в erp-kaspi сейчас отсутствует как понятие — при выборе модели стоит решить, нужен ли он отдельно от reversal.
- **Система может решить сама:** развести четыре пути в коде/UX. **Владельцу:** подтвердить таксономию (особенно: считать ли возврат Kaspi refund'ом — связано с OQ-006; допустим ли void как отдельная операция).

---

## Q6. Historical P&L preservation — как защищается P&L закрытых периодов

### Консенсус: P&L защищается закрытием периодов, а не флагом на документе

- **Oracle**: «When you finalize the activity for an accounting period, close the period to prevent the entry or posting of additional journal entries». Порядок: сначала субледжеры (AP/AR), потом GL. Постинг в открытый предшествующий период возможен (с пересчётом начальных остатков и корректировкой retained earnings при постинге в прошлый год), в закрытый — нет. Источник: https://docs.oracle.com/applications/farel12/financialsop_gs/FAUGL/FAUGL.pdf
- **SAP**: **OB52** (Posting Periods: Specify Time Intervals) — гейткипер: постинг в закрытый период **отклоняется**, а не молча сдвигается. Special periods 13–16 — для year-end корректировок после закрытия периода 12 (принадлежат старому году, но постятся при закрытом 12-м). **F.16 / FAGLGVTR** — carryforward: P&L-счета переносят сальдо в retained earnings; после carryforward и закрытия исторические цифры иммутабельны. Источники: https://www.stechies.com/amp/steps-closing-period-mm-fi-co/, https://github.com/boxlogodev/sapstack/blob/HEAD/./plugins/sap-fi/skills/sap-fi/SKILL.md
- **D365 Finance**: периоды **on hold** / **permanently closed**; плюс **year-end close** — второй, более сильный замок: если year-end close выполнен, транзакцию нельзя реверсировать, даже если период открыт (сначала нужно реверсировать сам year-end close). Источник: https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions
- **Business Central**: трёхуровневые окна **Allow Posting From/To** (компания → пользователь → шаблон журнала) + per-user override для постингa в закрытый период (с последующим восстановлением). Источник: https://learn.microsoft.com/is-is/dynamics365/business-central/localfunctionality/belgium/how-to-limit-the-posting-period
- **Odoo**: **Lock Everything date** / Journal Entries Lock Date — запрет создания/изменения записей на/до lock date; новые записи авто-датируются днём после lock date; исключение — только для администраторов/группы accountant. Источник: https://www.odoo.com/documentation/18.0/applications/finance/accounting/reporting/year_end.html
- **NetSuite**: закрытые периоды отклоняют GL-изменения жёсткими ошибками (TRAN_PERIOD_CLOSED, TRANS_EDIT_DISALLWD). Источник: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N3539978.html
- **QuickBooks**: **Closing date** + режим «warning» или «warning + password»; менять настройку могут только админы. Источник: https://quickbooks.intuit.com/learn-support/en-sg/help-article/close-books/close-books-quickbooks-online/L59LelyPM_SG_en_SG
- **Xero**: **Lock Dates** двух уровней (adviser lock — для всех; general lock — для не-adviser'ов). Источник: https://www.caman.au/wp-content/uploads/2023/06/Xero-Accounts-EOY-2023.pdf

### Вывод для erp-kaspi

Никто не «замораживает P&L флагом». Защита = **date-based posting lock**. Реверс старой операции всегда падает в **открытый** период (см. Q2) — поэтому закрытый период в отчётах не меняется. Для erp-kaspi это означает: P&L-запросы (`pnl()`, `v_ip_financial_summary`) трогать не нужно; нужен **posting-lock**, который проверяют и постинг, и реверс.

### Опции

| Опция | Pros | Cons |
|---|---|---|
| **A. Lock date на компанию/IP** (Odoo/QBO/Xero-школа) | Минимум схемы (одно поле); покрывает 95% кейсов малого бизнеса | Нет детализации по периодам/модулям |
| **B. Таблица posting_periods со статусами open/closed** (SAP/NetSuite-школа) | Точный контроль; отчётность по периодам; спец-периоды для year-end | Больше схемы и UI; нужен процесс закрытия периода |
| **C. Трёхуровневые окна + per-user override** (BC-школа) | Гибкость (бухгалтеру можно, остальным нельзя) | Сложность; override — дыра, требующая аудита |

### Impact на архитектуру

- Сейчас в erp-kaspi **нет никакого posting-lock** (проверено: в schema.sql есть только `payroll_periods`). Любой вариант — новая сущность + проверка в постинге операций и в `reverse_operation()`.
- D365F-прецедент «year-end close как второй замок» — опционально на будущее, не для первой итерации.
- **Система может решить сама:** начать с lock date (A) как минимально достаточного. **Владельцу:** подтвердить, что lock date достаточно (vs полноценные периоды), и кто вправе его менять/снимать.

---

## Q7. Idempotency/concurrency — как предотвращаются дублирующие реверсы

### Паттерны (все — state-based, ни у кого нет API idempotency-key для реверса)

- **Oracle**: guard по статусу — реверс допустим только для журнала «posted and not yet reversed» (Reversal Status). Явного idempotency-механизма в документации **не найдено**. Источник: https://docs.oracle.com/cd/G49759_01/trans/G46389-01/using-general-ledger.pdf
- **SAP**: re-validation состояния документа **внутри транзакции постинга** + SAP enqueue locks на документ: уже реверсированный / сам являющийся реверсом документ отклоняется («not a reversing document or already reversed»). Ручной offsetting-журнал в обход FB08 этот guard обходит — поэтому ручные сторно «possible but discouraged». Источники: SCEIS FB08 guide (см. Q1), https://community.sap.com/t5/application-development-discussions/payment-document-reversed/td-p/6586971
- **D365 Finance**: явный in-flight guard: «Transaction reversal is already in process… **A separate reversal process can't be done**» — пока идёт реверс, второй запустить нельзя. Источник: https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions
- **Business Central**: «An entry can only be reversed one time»; Cancel: «If the Canceled field is set to Yes, the posted sales invoice is already corrected or canceled». Источники: https://learn.microsoft.com/en-us/training/modules/correct-entries/4-reverse-journal-posting, https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice
- **NetSuite**: явные системные ошибки **TRANS_ALREADY_VOIDED** («Void failed because transaction is already voided»), **TRANS_ALREADY_REFUNDED**, **TRANS_ALREADY_SETTLED**. Источник: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N3539978.html
- **Odoo**: явного guard'а против двойного запуска reversal wizard в официальных доках **не найдено** (not confirmed); со стороны draft-коррекции — hash-lock постинга.
- **Xero / QuickBooks**: только валидации переходов состояний; idempotency-примитивов **не найдено**.

### Вывод для erp-kaspi

Индустрия полагается на **state guard внутри одной DB-транзакции** (проверка «не реверсирован» + штамп — атомарно) плюс явные коды ошибок повторных операций. erp-kaspi уже идёт **строже индустрии**: C-1 (commit 86f149b) ввёл `Idempotency-Key` → `uq_operations_import_fingerprint` на POST /operations. Для reversal при модели A/C рекомендуется та же комбинация:
1. `SELECT … FOR UPDATE` оригинала в одной транзакции;
2. отказ, если уже реверсирован или сам является reversal (по маркеру — см. Q1);
3. вставка reversal-документа + двусторонний штамп — атомарно;
4. idempotency-key на `POST /operations/{id}/reverse` (повтор с тем же ключом → вернуть существующий reversal, а не создавать второй).

**Система может решить сама:** всё перечисленное — техническая область, владелец не нужен. Единственное, что стоит показать владельцу: erp-kaspi здесь будет строже, чем Oracle/SAP/D365 (у них нет idempotency-key) — это плюс, а не отклонение от практики.

---

## 8. Сводная таблица: системы × вопросы

| Система | Модель реверса | Дата реверса | Реверс реверса | Двойной реверс | Защита периодов |
|---|---|---|---|---|---|
| Oracle GL | Compensating (Switch Dr/Cr / Change Sign) + статус Reversed | Параметр reversal period; только open/future-enterable | Запрещён | Статус-guard «posted and not yet reversed» | Close period блокирует постинг |
| SAP FI | Compensating reversal-документ + STBLG/XREVERSAL | Дата оригинала, если период открыт; иначе текущий открытый (reason 02/04) | Запрещён штатно; workaround — 3-й документ | Re-validation в транзакции + enqueue locks | OB52; special periods 13–16; carryforward F.16 |
| D365 Finance | Compensating reversal journal, linked | Дата оригинала или новая; только открытый период | **Разрешён явно** (цепочка) | In-flight guard «already in process» | On-hold/permanently closed; year-end close — 2-й замок |
| D365 BC | Гибрид: компенсация + corrective credit memo + Canceled-флаг | Та же дата и doc no., что у оригинала | Not confirmed | «Reverse only once»; Canceled-check | Allow Posting From/To (3 уровня) + per-user override |
| Odoo | Compensating (swap / Storno-negatives) + Reset to Draft | Текущая дата или дата оригинала; автосдвиг за lock date | Not confirmed | Not confirmed | Lock Everything / Journal Entries Lock Date |
| NetSuite | Конфигурируемо: zero-out void ИЛИ reversing journal + Voided | Дата voiding journal (может отличаться) | Not confirmed | TRANS_ALREADY_VOIDED / _REFUNDED | Closed periods; TRAN_PERIOD_CLOSED |
| QuickBooks | Void (zero-out) / Delete / Reverse (компенс. журнал) | Void хранит дату оригинала | Not confirmed | Not confirmed | Closing date + пароль |
| Xero | Status-flip VOIDED (без компенсирующих проводок); credit notes отдельно | Void без даты; lock date блокирует | Void терминален | Валидации переходов состояний | Lock Dates (2 уровня) |

---

## 9. Маппинг на Q1–Q7 владельца (из REVERSAL_DECISION_MEMO.md §9)

| Вопрос владельцу | Что говорит исследование | Кто решает |
|---|---|---|
| **Q1. Модель: (A) компенсация / (B) status flip / (C) гибрид** | Консенсус 7 систем: A или C. Чистый B не используется никем как финансовый механизм | **OWNER** — но теперь с референсом: B противоречит практике всех исследованных систем |
| **Q2. Тип компенсирующей записи (новый kind/code vs переиспользование); влияние на P&L (affects_pnl)** | Индустрия решает reversal reason'ами (SAP: флаги negative posting / alternative date) и Storno-настройкой (Odoo); прямого ответа «каким kind» для малого бизнеса нет | **OWNER** (бухгалтерское решение) |
| **Q3. Остаётся ли оригинал POSTED или →REVERSED; чем связывается пара** | Консенсус: оригинал остаётся posted-as-fact + маркер reversed + двусторонняя ссылка; `related_operation_id` переиспользовать нельзя (TD-018) — нужна отдельная связь | Маркер+ссылка — **система**; выбор конкретной схемы — система; подтверждение подхода — owner |
| **Q4. Частичный reversal** | Частичность естественно ложится на compensating-модель (Odoo: Reverse с редактируемым draft credit note; BC: корректировка строк credit memo). Status flip частичности не даёт | **OWNER** (нужен ли частичный), техника — система |
| **Q5. Отдельное поле reason** | Консенсус: да — SAP STGRD, D365/Odoo reason в reversal-документе; перезапись comment оригинала — антипаттерн (ср. TD-019) | **Система** (очевидно из практики) |
| **Q6. Отдельное право/подтверждение для reversal** | Oracle: journal approval rules (manager approval перед постингом); в erp-kaspi уже ACCOUNTANT + IP scope — индустрия не требует отдельного права везде, но approval-цепочка — норма | **OWNER** (достаточно ли ACCOUNTANT) |
| **Q7. Отображение пары в v_ip_financial_summary (свёрнуто/развёрнуто)** | SAP negative posting существует именно чтобы не раздувать валовые обороты; Odoo Storno — то же. Значит вопрос реальный, индустрия даёт оба инструмента | **OWNER** (свёрнуто vs развёрнуто), техника — система |

**Что система может решить сама, без владельца:** двусторонние ссылки + маркеры (Q1-детали, Q3-техника), поле reason (Q5), state-guard + idempotency-key + SELECT FOR UPDATE (Q7-tech), таксономия correction/reversal/void/refund в коде (Q5-tech), минимальный posting-lock как lock date (Q6 — с оговоркой), запрет реверса реверса по умолчанию (Q3 — Oracle/SAP-дефолт).

**Что действительно остаётся за OWNER:** выбор модели A vs C (B — только с сознательным отклонением от практики), бухгалтерский тип компенсирующей записи и её P&L-семантика (Q2), частичный reversal (Q4), уровень posting-lock (lock date vs периоды) и кто вправе его менять (Q6), свёрнуто/развёрнуто в отчётности (Q7), достаточно ли права ACCOUNTANT (Q6-memo), таксономия возвратов Kaspi (связано с OQ-006).

---

## 10. Источники (все URL, проверенные в ходе исследования)

**Oracle (docs.oracle.com — официальные):**
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/revers01.htm
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/reverse.htm
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/jeover.htm
- https://docs.oracle.com/cd/G49759_01/trans/G46389-01/using-general-ledger.pdf
- https://docs.oracle.com/applications/farel12/financialsop_gs/FAUGL/FAUGL.pdf
- https://docs.oracle.com/cd/E25178_01/fusionapps.1111/e20374/F484499AN19B45.htm
- https://docs.oracle.com/cd/E18727-01/doc.121/e12797.pdf (Payables: voiding payments)
- https://docs.oracle.com/cd/A85683_03/acrobat/115apug.pdf (Payables: invoice cancel)
- https://www.oracle.com/technetwork/fusion-apps/paycash-e22897-02-1514523.pdf (SLA event classes)
- https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N563543.html (NetSuite void)
- https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N1469460.html (NetSuite reversing journals)
- https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N3539978.html (NetSuite period errors / void errors)

**SAP (community.sap.com + операционные гайды; help.sap.com прямых страниц не отдал):**
- https://community.sap.com/t5/application-development-discussions/payment-document-reversed/td-p/6586971
- https://community.sap.com/t5/enterprise-resource-planning-q-a/how-to-display-all-reversal-document/qaq-p/2950003
- https://community.sap.com/t5/enterprise-resource-planning-q-a/reversal-reason/qaq-p/5787058
- https://community.sap.com/t5/enterprise-resource-planning-q-a/unable-to-reverse-fi-document-in-t-code-fb08/qaq-p/9218212
- https://community.sap.com/t5/enterprise-resource-planning-q-a/fbra-reversed-document-reversal-issue/qaq-p/12679926/comment-id/4129544
- https://community.sap.com/t5/enterprise-resource-planning-q-a/reversing-a-fi-document-related-to-sd/qaa-p/12210652
- https://cg.sc.gov/sites/cg/files/Documents/Guidance%20and%20Forms%20for%20State%20Agencies/CGs%20Accounting%20Policies%20and%20Procedures/Accounting%20Guidance/How%20to%20Record%20Document%20Reversals%20Using%20SCEIS%20Transaction%20Code%20FB08.pdf
- https://www.guru99.com/how-to-perform-document-reversal.html (третья сторона, шаги FB08)

**Microsoft (learn.microsoft.com — официальные):**
- https://learn.microsoft.com/en-us/dynamics365/finance/general-ledger/reverse-journal-posting
- https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions
- https://learn.microsoft.com/en-us/dynamics365/finance/asset-leasing/reverse-posted-lease-trans
- https://learn.microsoft.com/en-us/training/modules/correct-entries/4-reverse-journal-posting
- https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice
- https://learn.microsoft.com/pt-pt/dynamics365/business-central/finance-how-reverse-journal-posting
- https://learn.microsoft.com/en-us/dynamics365/business-central/production-how-to-reverse-output-posting
- https://learn.microsoft.com/is-is/dynamics365/business-central/localfunctionality/belgium/how-to-limit-the-posting-period

**Odoo:**
- https://www.odoo.com/documentation/18.0/applications/finance/accounting/reporting/year_end.html (официальные доки)
- https://hibou.io/docs/accounting-3/journal-entries-90/ver/14-0-21 (партнёрские доки по Odoo)
- https://www.cybrosys.com/blog/how-to-manage-reversal-of-the-journal-entries-in-odoo-17-storno-accounting

**QuickBooks / Xero (официальные help-центры):**
- https://quickbooks.intuit.com/learn-support/en-ca/help-article/list-management/void-delete-transactions-quickbooks-online/L5sZV8GYh_CA_en_CA
- https://plugin-qbo.intuit.com/qbo-ush/helpcontent/production/124/en_GB/QBO_Plus/Content/Topics/account/help_account_registers_delete.htm
- https://central.xero.com/0/article/Delete-or-void-a-sales-invoice

**Ограничения исследования:** help.sap.com не отдал прямых страниц по FB08 (факты SAP подтверждены community/гайдами, не официальными help-страницами — при подготовке ADR-016 стоит перепроверить по SAP Help Portal); по Odoo/QBO/Xero часть деталей — из help-центров и партнёрских доков, не из исходного кода; «not confirmed» помечено явно в тексте.

**Файлы детальных находок по системам:** `docs/research/reversal_oracle.md`, `docs/research/reversal_sap.md`, `docs/research/reversal_d365_odoo_others.md`.
