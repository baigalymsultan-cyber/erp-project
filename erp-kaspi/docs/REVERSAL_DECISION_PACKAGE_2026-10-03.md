# REVERSAL DECISION PACKAGE — 2026-10-03

**Статус:** research only. Код не изменён. Блокер REVERSAL_Q1_Q7 этим документом не закрывается —
это decision package для владельца: по каждому вопросу дана цепочка
`CURRENT CODE → EXTERNAL PRACTICE → OPTIONS → CONSEQUENCES → RECOMMENDED DEFAULT → OWNER DECISION`.

**Источники:** `docs/REVERSAL_DECISION_MEMO.md` (Q1–Q7, §9), `docs/REVERSAL_RESEARCH_2026-10-03.md`,
`docs/research/reversal_oracle.md`, `docs/research/reversal_sap.md`, `docs/research/reversal_d365_odoo_others.md`,
официальная документация Oracle / SAP / Microsoft Learn / Odoo (URL — в разделе «Источники»).

**Как читать:** для каждого Q — сначала факты текущего кода (проверены чтением кода 2026-10-03),
затем практика 7 production-систем, затем опции с последствиями, затем **рекомендуемый дефолт
с обоснованием** и в конце — **точный вопрос владельцу** (только то, что нельзя вывести из evidence).

**Главная цель документа:** минимизировать множество owner-решений. Помечено явно:
🟢 — система решает сама (есть evidence-based default); 🟡 — владелец подтверждает дефолт;
🔴 — только владелец (нет объективного ответа).

---

## Q1. Модель: (A) компенсирующая операция / (B) status flip / (C) гибрид

### CURRENT CODE

`app/services/__init__.py:505` `reverse_operation(session, operation_id, comment=None)`:
1. `session.get(Operation, operation_id)`; нет → `ValueError`.
2. Проверка transfer legs (`transfers.outbound_operation_id / inbound_operation_id`) → leg реверсировать нельзя.
3. Требует `op.status == "POSTED"`, иначе `ValueError`.
4. **Мутирует ту же строку**: `op.status = "REVERSED"`; при `comment` — **перезаписывает** `op.comment`.
5. `session.flush()`; возвращает `{"id", "status"}`.
6. Компенсирующая операция **не создаётся**, `related_operation_id` **не заполняется**.

API `POST /operations/{operation_id}/reverse` (`app/api/__init__.py:266`): привилегия `ACCOUNTANT`,
`ensure_ip_access` на `target.ip_id`, 404 если нет, `session.commit()`.

P&L: `pnl()` и `v_ip_financial_summary` учитывают только `status='POSTED'` — reversal работает
**исключением из итогов**, а не компенсацией.

Защита БД: `fn_protect_posted_ledger_operation` запрещает изменение финансовых полей POSTED/REVERSED-строк;
переход `POSTED → REVERSED` разрешён как **временная совместимость** до ADR-016.
Тесты `test_ledger_immutability.py` / `test_api.py` фиксируют текущее поведение как временный контракт.

### EXTERNAL PRACTICE

| Система | Модель | Источник |
|---|---|---|
| Oracle GL (EBS/Fusion) | Compensating: новая проводка (Switch Dr/Cr — дефолт для аккруалов; Change Sign — для ошибок ввода). Исходник неизменен; view-only статус `Reversal Status` | https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/revers01.htm |
| SAP FI | Compensating: FB08/F.80 постят новый reversal-документ; оригинал штампуется STBLG/XREVERSAL=1, реверс XREVERSAL=2; двусторонняя связь | https://community.sap.com/t5/application-development-discussions/payment-document-reversed/td-p/6586971 |
| D365 Finance | Compensating: reversal journal, зеркалящий оригинал, связанный с исходным header | https://learn.microsoft.com/en-us/dynamics365/finance/general-ledger/reverse-journal-posting |
| D365 Business Central | **Гибрид**: corrective credit memo (компенсация) + флаг `Canceled=Yes` на оригинале | https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice |
| Odoo | Compensating: Reverse Entry wizard (зеркало) или Storno (отрицательные суммы); ссылка на оригинал | https://www.odoo.com/documentation/18.0/applications/finance/accounting/reporting/year_end.html |
| NetSuite | Конфигурируемо: zero-out Void **или** reversing journal + статус Voided (гибрид) | https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N1469460.html |
| QuickBooks Online | Раздельно: Void (zero-out) / Delete / Reverse (компенсирующий журнал) | https://quickbooks.intuit.com/learn-support/en-ca/help-article/list-management/void-delete-transactions-quickbooks-online/L5sZV8GYh_CA_en_CA |
| Xero | Status-flip VOIDED на документе (без компенсирующих проводок); финансовый эффект — отдельным credit note | https://central.xero.com/0/article/Delete-or-void-a-sales-invoice |

**Критическое сравнение (не копирование Oracle):** Oracle-подход (Switch Dr/Cr) спроектирован под
двусторонний ledger с дебетами/кредитами — в erp-kaspi такой структуры нет (односторонние операции
с direction IN/OUT). Прямое копирование невозможно; переносится **принцип** (компенсация новым
документом), а не механика. SAP ближе по духу (документная модель + причины реверса), BC/NetSuite —
по гибридной схеме «компенсация + флаг». Xero — единственный status-flip, но у него вообще нет
компенсирующих проводок в модели (малый бизнес, упрощённый учёт) — и даже он разделяет void документа
и credit note для денег.

### OPTIONS

- **A. Compensating journal** — новая POSTED-операция противоположного эффекта; оригинал неизменен.
- **B. Status flip** — оставить текущий код (узаконить).
- **C. Гибрид** — компенсация (A) + маркер «reversed» на оригинале + двусторонняя ссылка.

### CONSEQUENCES

| | A | B | C |
|---|---|---|---|
| Аудит-трейл | Полный (два документа + связь) | Только статус + перезаписанный comment (TD-019) | Полный + читаемый флаг |
| Частичный reversal | Возможен (сумма параметром) | Невозможен | Возможен |
| Master Prompt §1 / ADR-007 | Соответствует | Противоречит (нужен пересмотр ADR-007) | Соответствует |
| Прецеденты | 6/7 систем | 0/7 как финансовый механизм | 3/7 (BC, NetSuite, SAP-аннотация) |
| Изменения кода | Средние (тип компенсации, ссылка, reason) | Нулевые | Средние+ (флаг/маркер) |
| Риск двойного учёта | Нет | Нет | Только при ошибке фильтров P&L |

### RECOMMENDED DEFAULT 🟡

**C (гибрид)** — компенсация как финансовый механизм + маркер на оригинале.
Обоснование: консенсус индустрии — компенсация (A), но три системы добавляют флаг-аннотацию (BC Canceled,
NetSuite Voided, SAP XREVERSAL, Oracle Reversal Status) именно для читаемости и state-guard'а.
Минимальный C = A + маркер — это строго надмножество A, без его недостатков. Чистый B не имеет
прецедента ни в одной из семи систем как способ отмены posted-транзакции.

### OWNER DECISION 🔴

**Подтвердить A vs C** (рекомендован C). Если владелец выбирает B — это сознательное отклонение
от практики всех исследованных систем + требуется пересмотр ADR-007 (статус ACCEPTED).

---

## Q2. Тип компенсирующей записи: новый kind/code vs переиспользование; влияние на P&L (affects_pnl)

### CURRENT CODE

Компенсаций не существует. CHECK `chk_operation_types_dir_kind` (`database/schema.sql:759`):
`INCOME→IN`, `EXPENSE→OUT`, `TRANSFER→IN/OUT`. Компенсация продажи `SALE_KASPI_SHOP` (IN/INCOME)
требует движения OUT — под существующие kind'ы не ложится. `related_operation_id` занят
transfer-парингом (TD-018 запрещает переиспользовать). Seed типов компенсации отсутствует.

### EXTERNAL PRACTICE

- **SAP**: тип компенсации не фиксирован — решает **reversal reason** (STGRD, коды 01/02/03/04/ZU…).
  Ключевой механизм — флаг **«Negative postings allowed»** на причине: обычная причина постит реверс
  противоположной стороной (обороты счёта растут), причина с negative posting — той же стороной
  с отрицательной суммой (обороты уменьшаются, нетто ноль). То есть SAP отвечает на Q2 не «каким типом»,
  а «каким режимом проводки».
  Источник: https://www.scribd.com/document/98728634/Sap-FB08-F-80-Tutorial-Document-Reversal (reason codes 01/02/ZU);
  https://community.sap.com/t5/enterprise-resource-planning-q-a/reversal-reason/qaq-p/5787058
- **Oracle**: два метода — **Switch Dr/Cr** (дебет↔кредит, дефолт, для аккруалов) и **Change Sign**
  (та же сторона, отрицательная сумма, для ошибок ввода). Дефолт в Fusion: Switch Dr/Cr.
  Источник: https://www.oracle.com/technetwork/fusion-apps/fincontrolandreporting-e22895-02-1514520.pdf
- **Odoo**: обычный Reverse (swap сторон) vs **Storno accounting** (копия с отрицательными суммами;
  обязательно в ряде EU-локализаций).
  Источник: https://www.cybrosys.com/blog/how-to-manage-reversal-of-the-journal-entries-in-odoo-17-storno-accounting
- **Критическое замечание:** все три механизма (negative posting, Change Sign, Storno) предполагают
  двусторонний ledger, где «сторона» имеет смысл. В erp-kaspi операции односторонние (direction IN/OUT,
  kind INCOME/EXPENSE) — «отрицательная сумма» конфликтовала бы с §27 sign semantics. Прямой перенос
  невозможен; переносится идея: **компенсация — это отдельная учётная сущность с явной семантикой**,
  а не «минус та же строка».

### OPTIONS

- **A. Новый kind** (напр. `REVERSAL`) + новые коды типов (`REVERSAL_SALE_*`): CHECK меняется,
  семантика `affects_pnl` задаётся явно. Плюс: бухгалтерски чисто («возврат ≠ расход» — ср. SAP:
  credit memo ≠ expense). Минус: миграция CHECK + seed + P&L-логика.
- **B. Переиспользование EXPENSE** для компенсации INCOME (и наоборот): ноль миграций.
  Минус: возврат продажи станет «расходом» в отчётности — бухгалтерски сомнительно, искажает
  структуру P&L (возвраты смешаются с реальными расходами).
- **C. Компенсация тем же типом, но противоположным direction** (нарушает CHECK — требует его
  ослабления): технически просто, семантически грязно — тип `SALE_KASPI_SHOP` с direction OUT
  означает «продажа наоборот», что затемняет отчётность.

### CONSEQUENCES

Выбор определяет: (1) что попадёт в `v_ip_financial_summary` как доход/расход;
(2) нужен ли `affects_pnl` у компенсации (если kind=REVERSAL с affects_pnl=false — пара вообще
не тронет P&L, что неверно для реального возврата денег); (3) читаемость оборотов.

### RECOMMENDED DEFAULT 🟡

**A (новый kind + явные коды типов)** — единственный вариант, не искажающий P&L-структуру.
Обоснование: SAP/Oracle/Odoo все выделяют компенсацию в отдельную сущность с собственными
правилами; ни одна система не маппит сторно на «расход». Переиспользование EXPENSE — это именно
та выдуманная семантика, которую запрещает правило финансовой безопасности.

### OWNER DECISION 🔴

1. **Бухгалтерская классификация компенсации**: новый kind (напр. `REVERSAL`) vs иное?
   Является ли возврат продажи «отрицательным доходом», отдельной категорией или расходом?
2. **`affects_pnl` компенсации**: true (пара видна в P&L, нетто ноль) — рекомендовано;
   false только если компенсация — чисто техническая сторно-запись (тогда нужен отдельный
   механизм учёта реального движения денег — см. таксономию refund).
3. Именование кодов типов (напр. `REVERSAL_SALE_KASPI_SHOP`) — владелец или делегирует системе.

---

## Q3. Оригинал: остаётся POSTED или → REVERSED? Чем связывается пара?

### CURRENT CODE

Оригинал мутирует `POSTED → REVERSED`; связи нет. Триггер `trg_operations_protect_posted_ledger`
защищает финансовые поля POSTED/REVERSED-строк, но разрешает этот переход как временную меру.

### EXTERNAL PRACTICE

Консенсус 6/7: **оригинал остаётся posted-as-fact + получает маркер + двустороннюю ссылку**:

| Система | Маркер на оригинале | Двусторонняя ссылка |
|---|---|---|
| Oracle | View-only `Reversal Status` = `Reversed` (было `Not reversed`) — блокирует повторный реверс | Имя batch `Reverses <name> <date> <time>` |
| SAP | `XREVERSAL=1` (reversed), у реверса `XREVERSAL=2` (reversal document) | `BKPF-STBLG` / `STJAH` (номер reversal-документа обе стороны) |
| D365F | — (trace через linked journal) | Reversal journal ↔ original journal header; диалог Reverse tracing |
| BC | `Canceled=Yes` | Corrective credit memo связан с оригиналом |
| Odoo | Метка «Reversed.» | `reversed_entry_id`; Reference «Reversal of: <ref>» |
| NetSuite | Статус `Voided` + `Voided On` | `Void Of` (реверс → оригинал) |
| Xero | `VOIDED` (терминальный) | — (нет компенсации) |

### OPTIONS

- **A. Оригинал остаётся POSTED + boolean/маркер + ссылка** — P&L-слой не трогаем вообще:
  пара POSTED+POSTED даёт нетто ноль; маркер — только для UI/аудита/guard'а.
- **B. Оригинал → REVERSED (аннотация) + компенсация POSTED** — как сейчас + компенсация.
  Риск: P&L-фильтры должны одновременно исключать REVERSED и включать компенсацию —
  два механизма на один эффект; при рассинхроне фильтров — двойной учёт (см. memo §5, вариант C).
- **C. Отдельная колонка-флаг `is_reversed`** вместо переиспользования статуса.

### CONSEQUENCES

Вариант A: статус остаётся источником P&L-правды (один механизм), маркер — источником
читаемости. Вариант B: два источника правды об «активности» операции — хрупко.
`related_operation_id` переиспользовать нельзя (TD-018) — нужна отдельная колонка
`reversal_of_id` (self-reference на `operations.id`), опционально `reversed_by_id` для
обратного направления или view.

### RECOMMENDED DEFAULT 🟢

**A: оригинал остаётся POSTED (иммутабелен — триггер уже гарантирует), маркер + `reversal_of_id`.**
Обоснование: это ровно консенсус индустрии (posted-as-fact + marker + link); технически
минимально (P&L-слой не меняется — пара нетится сама); устраняет двойной источник правды.
Система решает сама — owner лишь подтверждает.

### OWNER DECISION 🟡

Подтвердить: **«оригинал остаётся POSTED навсегда, reversal — новая POSTED-операция со ссылкой»**.
(Если владелец хочет B — это допустимо, но потребует аккуратной синхронизации P&L-фильтров.)

---

## Q4. Частичный reversal (часть суммы)

### CURRENT CODE

Невозможен: status flip — это «вся строка или ничего».

### EXTERNAL PRACTICE

| Система | Частичный reversal | Как |
|---|---|---|
| SAP | FB08 — **только полный**; частичность — через credit/debit memo (отдельный бизнес-документ) | https://community.sap.com/t5/enterprise-resource-planning-q-a/reversing-a-fi-document-related-to-sd/qaa-p/12210652 |
| Odoo | **Да**: Reverse wizard создаёт draft-зеркало, которое **можно отредактировать перед постингом** (сумма правится) | https://hibou.io/docs/accounting-3/journal-entries-90/ver/14-0-21 |
| D365 BC | **Да**: corrective credit memo — построчно, частичные суммы естественны | https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice |
| Oracle | Полный журнал; частичность — ручным журналом | — |
| NetSuite | Partial void отдельных строк — да (void по строкам) | https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N563543.html |

**Вывод:** частичность естественно ложится на compensating-модель и неестественна для status flip.
SAP проводит границу: ошибка = полный reversal; бизнес-коррекция (частичная) = credit memo.

### OPTIONS

- **A. Разрешить**: параметр `amount ≤ original.amount`; ссылка та же; guard «сумма всех реверсов ≤ оригинала».
- **B. Запретить**: только полный reversal; частичные коррекции — отдельными операциями вручную.

### CONSEQUENCES

A требует guard'а суммарной компенсации (иначе «пересторно»); B проще, но для возврата части
суммы клиенту (типичный Kaspi-кейс — частичный возврат заказа, ср. OQ-006) придётся городить
обходные пути.

### RECOMMENDED DEFAULT 🟡

**A (разрешить частичный)** — с guard'ом суммы. Обоснование: частичные возвраты — штатный
сценарий розницы/Kaspi; Odoo/BC/NetSuite это поддерживают; status flip всё равно уходит.

### OWNER DECISION 🔴

**Нужен ли частичный reversal как бизнес-операция?** (Да/Нет.) Техника при «да» — за системой.

---

## Q5. Отдельное поле reason вместо перезаписи comment

### CURRENT CODE

`reverse_operation(..., comment=None)`: при переданном `comment` **перезаписывает** `op.comment`
у POSTED-строки (TD-019 — остаточная изменяемость provenance).

### EXTERNAL PRACTICE

Консенсус абсолютный: причина — **отдельное поле на reversal-документе**, оригинал не трогают:

| Система | Поле причины |
|---|---|
| SAP | **STGRD** — reversal reason (коды 01/02/03/04/ZU…), обязателен в FB08; управляет датой и negative posting |
| Oracle | Reason — параметр reversal batch; попадает в имя/отчёты |
| D365F / BC | Reason в диалоге reversal; у BC — в corrective credit memo |
| Odoo | Reason из wizard → в Reference реверса («Reversal of: <ref>, <reason>») |
| NetSuite | Мемо/причина на reversing journal |

### OPTIONS

- **A. Новая колонка** `reversal_reason` (или `reason`) на `operations`, заполняется только у reversal-операций.
- **B. Оставить comment** (перезапись) — текущий антипаттерн.

### CONSEQUENCES

B уничтожает provenance исходной строки (TD-019) — прямо противоречит §16 «history is kept».
A — тривиальная миграция (nullable колонка), ноль влияния на P&L.

### RECOMMENDED DEFAULT 🟢

**A — отдельная колонка; перезапись comment оригинала запретить.** Система решает сама;
владелец не нужен. (Опционально владелец может дать справочник кодов причин — как SAP STGRD, —
но это улучшение, не блокер.)

### OWNER DECISION 🟢

Не требуется. (Информирование: так делают все 7 систем.)

---

## Q6. Отдельное право/подтверждение для reversal (сверх ACCOUNTANT)

### CURRENT CODE

`require_privilege("ACCOUNTANT")` + `ensure_ip_access` на IP операции. Отдельного права нет,
второго подтверждающего нет, approval-цепочки нет.

### EXTERNAL PRACTICE

| Система | Кто может реверсировать |
|---|---|
| Oracle | **Journal approval rules**: reversal batch проходит тот же approval workflow, что и обычный журнал (manager approval до постинга); reversal period контролируется отдельно |
| SAP | Авторизационный объект на FB08 (F_BKPF_BUK / reversal reason); обычно — роль бухгалтера; отдельной «супер-роли» нет, но reason 02 (closed period) часто ограничивают |
| D365F / BC | Ролевая модель; BC: per-user posting windows — бухгалтеру можно в закрытый период, остальным нет |
| Odoo | Lock date: постинг до lock date — только группа **Adviser/Accountant** (исключение из lock) |
| QuickBooks | **Closing date меняет только Primary/Company Admin**; правки в закрытом периоде — с паролем; Closing Date Exception Report показывает обходы |
| Xero | Двухуровневые lock dates: adviser lock (все) vs general lock (не-adviser'ы) |

**Паттерн:** право на reversal = роль бухгалтера (как сейчас в erp-kaspi) + **отдельно защищённая
операция — изменение posting lock** (только админ). Approval-цепочка (второй человек) — норма
для enterprise (Oracle), но не для малого бизнеса (QBO/Xero/Odoo).

### OPTIONS

- **A. Оставить ACCOUNTANT + IP scope** (текущее) — соответствует малому бизнесу.
- **B. Отдельная привилегия** `ACCOUNTANT_REVERSAL` — гранулярность без второй подписи.
- **C. Двойное подтверждение** (maker-checker: создал reversal — другой ACCOUNTANT подтверждает) — enterprise-подход.

### CONSEQUENCES

A: ноль изменений; риск — один человек может сторнировать любую операцию своего IP.
B: малая миграция ролей; гибкость. C: новый workflow (статус PENDING_APPROVAL), задержки в операционке.

### RECOMMENDED DEFAULT 🟡

**A (ACCOUNTANT достаточно) + отдельно: изменение lock date — только ADMIN** (см. deep-dive «Posting lock»).
Обоснование: для малого бизнеса (ЦА erp-kaspi) maker-checker избыточен; индустрия малого бизнеса
(QBO/Xero/Odoo) обходится ролями + защитой lock date. Enterprise-цепочки — не целевой сегмент.

### OWNER DECISION 🔴

1. **Достаточно ли ACCOUNTANT для reversal, или нужно отдельное право / второе подтверждение?**
2. **Кто вправе менять lock date** (рекомендовано: только ADMIN; изменение — в audit_log)?

---

## Q7. Отображение пары в v_ip_financial_summary: свёрнуто vs развёрнуто

### CURRENT CODE

`v_ip_financial_summary` и `pnl()` фильтруют `status='POSTED'`. При модели A/C пара
(оригинал POSTED + компенсация POSTED) даст **нетто ноль** автоматически, но **валовые обороты
вырастут** (продажа 100 000 + сторно 100 000 = оборот 200 000 при нетто 0).

### EXTERNAL PRACTICE

- **SAP**: именно для этого существует **negative posting** (Storno): причина реверса с флагом
  «Negative postings allowed» постит реверс **той же стороной с отрицательной суммой** —
  обороты счёта **уменьшаются**, нетто ноль, вал не раздувается. Обычная причина — наоборот,
  раздувает обороты (это иногда нужно для аудита валовых потоков).
  Источник: https://www.scribd.com/document/98728634/Sap-FB08-F-80-Tutorial-Document-Reversal
- **Oracle**: Switch Dr/Cr (раздувает обороты) vs Change Sign (не раздувает) — выбор метода
  решает тот же вопрос на уровне проводки.
- **Odoo**: Storno (отрицательные суммы) — тот же эффект, обязателен в ряде EU-локализаций.

**Критическое замечание для erp-kaspi:** negative posting / Change Sign / Storno предполагают
двусторонний ledger и поддержку отрицательных сумм. В erp-kaspi §27 sign semantics + CHECK
`chk_operation_types_dir_kind` — «отрицательная компенсация» потребовала бы пересмотра знаковой
семантики. Переносить механику нельзя; вопрос остаётся на уровне **представления в отчётности**.

### OPTIONS

- **A. Нетто по умолчанию**: view суммирует как есть — пара нетится в ноль; валовые обороты растут.
  Детализация — в журнале операций (обе строки видны). Ноль изменений view.
- **B. Отдельная строка «сторно» в отчётности**: view/отчёт показывают reversal-пары отдельной
  агрегированной строкой (валовые обороты видны, но не смешаны с «чистыми»).
- **C. SAP-style negative amounts**: компенсация — отрицательная сумма тем же типом; требует
  пересмотра §27 и CHECK'ов — **тяжёлая семантическая смена, не рекомендована** без явного
  бухгалтерского требования.

### CONSEQUENCES

A: просто; но «оборот по ИП» в управленческой отчётности будет включать сторно.
B: прозрачнее для владельца бизнеса («сколько вернули»), чуть сложнее view.
C: ломает знаковую семантику всей системы — только по explicit бухгалтерскому решению.

### RECOMMENDED DEFAULT 🟡

**A (нетто; детализация в журнале)**, с закладкой на B (отдельная строка «возвраты/сторно»
в управленческих отчётах — легко добавляется позже агрегацией по kind=REVERSAL).

### OWNER DECISION 🔴

**Устраивает ли нетто-представление, или нужна отдельная видимость сторно в сводке?**
(Техника при любом ответе — за системой; C — только по explicit запросу.)

---

# PART B — DEEP DIVES

## D1. Reversal date: business date vs system date

**Вопрос:** какой датой датировать компенсирующую операцию — датой исходной операции
(business date оригинала), текущей датой (system date) или параметром?

**Практика:**

| Система | Дефолт | Опции | Источник |
|---|---|---|---|
| Oracle | Параметр **reversal period**: Same / Next / Next Non-Adjusting Period | Период обязан быть open или future-enterable | https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/revers01.htm |
| SAP | **Дата оригинала** (пустое поле Posting Date = дата исходника), если период открыт (OB52) | Reason 01: авто-дата = последний день текущего периода; 02: другой период (закрытый исходный); ZU: дата пользователя | https://cg.sc.gov/sites/cg/files/Documents/Guidance%20and%20Forms%20for%20State%20Agencies/CGs%20Accounting%20Policies%20and%20Procedures/Accounting%20Guidance/How%20to%20Record%20Document%20Reversals%20Using%20SCEIS%20Transaction%20Code%20FB08.pdf |
| D365F | Диалог: Yes = сохранить даты оригинала / No = ввести новую | Новая дата обязана быть в открытом периоде | https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions |
| BC | **Та же дата и тот же номер документа**, что у оригинала (жёстко) | Окна Allow Posting From/To гейтят | https://learn.microsoft.com/pt-pt/dynamics365/business-central/finance-how-reverse-journal-posting |
| Odoo | **Specific = сегодня** (дефолт) | Journal Entry Date = дата оригинала; автосдвиг за lock date | https://hibou.io/docs/accounting-3/journal-entries-90/ver/14-0-21 |
| NetSuite | Дата voiding journal (может отличаться); preference для закрытых периодов | Void из закрытого периода — только с reopen или variance-date preference | https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N1469460.html |
| QBO | Void **хранит дату оригинала** | Closing date + пароль блокируют | https://quickbooks.intuit.com/learn-support/en-ca/help-article/list-management/void-delete-transactions-quickbooks-online/L5sZV8GYh_CA_en_CA |
| Xero | Void без даты; фактическая коррекция — **credit note датой после lock date** | Lock date — жёсткий гейт | https://central.xero.com/0/article/Delete-or-void-a-sales-invoice |

**Два лагеря:** (1) «дата оригинала, если период открыт» (SAP, D365F, BC, QBO) — коррекция остаётся
в том же периоде; (2) «текущая дата» (Odoo) — коррекция всегда в текущем периоде.
**Общий знаменатель: дата реверса — это business date (accounting date), никогда не «дата нажатия кнопки»
как технический штамп; и она никогда не попадает в закрытый период.**

**Для erp-kaspi:** у операции уже есть `operation_date` (business date). Компенсация — новая строка,
ей нужен собственный `operation_date` = параметр `reversal_date`.

**RECOMMENDED DEFAULT 🟢:** `reversal_date` — параметр API, дефолт = текущая дата (Odoo-школа,
проще и безопаснее без period-контроля); валидация `reversal_date > lock_date`.
Backdate в открытый прошлый период — опционально позже.

**OWNER DECISION 🟡:** разрешить ли backdate реверса в прошлый **открытый** период (SAP-школа),
или всегда текущая дата (Odoo-школа)?

## D2. Same-period vs next-period: что происходит на закрытии периода

**Практика:** ни одна система не позволяет реверсу попасть в закрытый период. Механизмы:
- **Oracle**: reversal period — явный параметр; closed → отказ.
- **SAP**: reason 02 («Reversal in closed period») — реверс постится в **более поздний открытый период**;
  на year-end: «ensure reversals are posted in the same fiscal year as the original document»
  (два фискальных года могут быть открыты одновременно).
- **D365F**: смена reversal date на открытый период; **year-end close — второй, более сильный замок**:
  после year-end close реверс невозможен, даже если период открыт (надо сначала реверсировать сам year-end close).
- **NetSuite**: void из закрытого периода — только после reopen'а периода.
- **Odoo/QBO/Xero**: lock date / closing date — реверс авто-сдвигается за lock date или отклоняется.

**Для erp-kaspi:** сейчас period-контроля нет вообще (только `payroll_periods`). Минимально
достаточно lock date (см. D6). Правило: `reversal_date > lock_date`, иначе отказ с понятной ошибкой.

**RECOMMENDED DEFAULT 🟢:** lock date как гейт; реверс «старого» — в текущий открытый период.
**OWNER DECISION 🟡:** lock date vs полноценные периоды (см. D6).

## D3. Partial reversal: кто разрешает и как

См. Q4. Дополнительно: **SAP проводит границу** — FB08 (reversal ошибочной проводки) всегда полный;
частичная бизнес-коррекция — это **credit/debit memo**, отдельный документ с собственной нумерацией
и approval. Odoo стирает границу (draft-зеркало правится). Для erp-kaspi с его Kaspi-возвратами
(частичный возврат заказа — штатный сценарий, OQ-006) граница SAP «ошибка vs бизнес-коррекция»
ложится на таксономию D5: **reversal = полный (ошибка); частичный возврат денег = refund**.

**RECOMMENDED DEFAULT 🟡:** разрешить частичный reversal технически (amount ≤ остатка),
но в UX/таксономии разделять «сторно ошибки» и «частичный возврат» (см. D5).
**OWNER DECISION 🔴:** см. Q4.

## D4. Reversal of reversal: почему запрещён; исключение D365

**Практика:**

| Система | Реверс реверса | Источник |
|---|---|---|
| Oracle Fusion | **Запрещён**: «Reversal journals cannot be reversed». Даже если reversal удалён не запощенным — запрет сохраняется. Выход: скопировать оригинальный журнал и отредактировать | https://www.oracle.com/technetwork/fusion-apps/fincontrolandreporting-e22895-02-1514520.pdf |
| SAP | **Запрещён штатно** (FB08: «not a reversing document or already reversed»); workaround — **третий ручной документ** | https://cg.sc.gov/sites/cg/files/Documents/Guidance%20and%20Forms%20for%20State%20Agencies/CGs%20Accounting%20Policies%20and%20Procedures/Accounting%20Guidance/How%20to%20Record%20Document%20Reversals%20Using%20SCEIS%20Transaction%20Code%20FB08.pdf |
| D365 Finance | **Разрешён явно** — единственный: «a reversed transaction can be reversed again (that is, the reversal can be reversed)», цепочка с trace-ссылками | https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions |
| BC | «An entry can only be reversed one time» | https://learn.microsoft.com/en-us/training/modules/correct-entries/4-reverse-journal-posting |
| Xero | Void терминален (un-void невозможен; только копирование в новый документ) | https://central.xero.com/0/article/Delete-or-void-a-sales-invoice |

**Почему запрещают (анализ):** (1) читаемость аудита — цепочка A→B→C→D экспоненциально усложняет
проверку; (2) состояние «реверсированности» становится рекурсивным; (3) риск зацикливания в
автоматических процессах (AutoReverse). D365 разрешает, потому что у него journal-trace модель
изначально цепочечная — но даже там это edge-case, а не норма.

**RECOMMENDED DEFAULT 🟢:** запретить (Oracle/SAP-консенсус 6/7); ошибочный reversal правится
третьей ручной компенсирующей операцией (SAP-паттерн). Guard: отказ, если операция сама является
reversal (нужен маркер — см. Q3).

**OWNER DECISION 🟢:** не требуется (техническое решение; владелец информируется).

## D5. Таксономия: refund vs reversal vs correction vs void

Четыре разных кодовых пути во всех системах. Смешивание в один `reverse_operation()` — антипаттерн.

| Операция | Когда | Механизм | Пример для erp-kaspi |
|---|---|---|---|
| **Correction** (исправление) | Документ **не запощен** (или меняются нефинансовые поля) | Правка in place | Опечатка в comment DRAFT-операции; правка суммы до постинга. В erp-kaspi: DRAFT-операции правимы (триггер защищает только POSTED) |
| **Reversal** (сторно) | **POSTED**, запись ошибочна | Компенсирующая операция (Q1-C) | Дублированная продажа; не та сумма; не тот тип |
| **Void** (аннулирование) | Документ/платёж аннулируется как бизнес-факт; запись **сохраняется** | Статус-флаг (+ часто авто-компенсация); **терминален** — void необратим (Oracle AP: «you cannot reverse a void») | Аннулированный чек; отменённый платёж. В erp-kaspi отсутствует как понятие |
| **Refund** (возврат денег) | Деньги реально возвращаются контрагенту | **Отдельный платёжный документ**, никогда не реверс инвойса (SAP, NetSuite: TRANS_ALREADY_REFUNDED — один refund на транзакцию) | **Возврат продажи Kaspi** (OQ-006): деньги ушли клиенту — это refund (OUT-операция), а не reversal ошибочной проводки |

**Ключевой вывод для erp-kaspi:** «возврат продажи Kaspi» — это **refund-сценарий**, а не reversal.
Проектировать reversal API под возвраты — ошибка таксономии (SAP: «reversal — для ошибочных
проводок; во всех остальных случаях — credit/debit memo»). Void как отдельная операция в erp-kaspi
отсутствует — при выборе модели стоит решить, нужен ли он.

**RECOMMENDED DEFAULT 🟢:** развести четыре пути в коде/UX/API; reversal — только для ошибочных
POSTED; Kaspi-возвраты — refund (связано с OQ-006).
**OWNER DECISION 🟡:** подтвердить таксономию; нужен ли void как отдельная операция.

## D6. Posting lock: lock date vs period close; кто меняет

**Практика:**

| Система | Механизм | Кто меняет |
|---|---|---|
| Oracle | Close period (сначала субледжеры, потом GL); постинг в закрытый — невозможен | Бухгалтер периода (роль) |
| SAP | **OB52** — posting periods (жёсткий reject, не сдвиг); special periods 13–16 для year-end; F.16 carryforward | FI-конфигурация (консультант/админ) |
| D365F | Periods on hold / permanently closed; **year-end close — второй замок** (сильнее периодов) | Финансовая роль; year-end close — отдельная процедура |
| BC | **Allow Posting From/To** — 3 уровня (компания → пользователь → шаблон журнала) + per-user override | Override — у указанных пользователей; восстанавливается после |
| Odoo | **Lock Everything / Journal Entries Lock Date**; исключение — группа Adviser/Accountant | Админ/бухгалтер |
| NetSuite | Closed periods → жёсткие ошибки **TRAN_PERIOD_CLOSED**, **TRANS_EDIT_DISALLWD** | Админ |
| QBO | **Closing date + пароль**; только **Primary/Company Admin** может менять; **Closing Date Exception Report** показывает обходы | Только админ |
| Xero | Два уровня: adviser lock (все) / general lock (не-adviser'ы) | Adviser |

**Консенсус:** защита P&L — это **date-based lock**, а не флаг на документе. Минимум — одна дата
(Odoo/QBO/Xero-школа); максимум — таблица периодов со статусами (SAP/NetSuite-школа).

**Для erp-kaspi:** сейчас lock'а нет. Минимально достаточно: `lock_date` (на IP или компанию) +
проверка во всех постингах и в reversal (`operation_date > lock_date`), + изменение lock date
только ADMIN с записью в audit_log, + отчёт «операции с датой ≤ lock_date, созданные после»
(аналог Closing Date Exception Report).

**RECOMMENDED DEFAULT 🟢:** lock date (Odoo/QBO-минимализм); меняет только ADMIN; все изменения — в audit_log.
**OWNER DECISION 🟡:** lock date достаточно, или нужны полноценные периоды (SAP-школа)?
Кто именно вправе менять (ADMIN — рекомендовано)?

## D7. Audit trail: какие поля; что иммутабельно

**Минимальный набор из консенсуса:**
1. **Двусторонняя ссылка** оригинал ↔ reversal (запрос «все реверсы» — одним фильтром):
   SAP STBLG/STJAH; Oracle имя batch; Odoo reversed_entry_id; NetSuite Void Of/Voided On.
2. **Маркер на обеих сторонах**: SAP XREVERSAL 1/2; Oracle Reversal Status; BC Canceled; NetSuite Voided.
3. **Причина** — отдельное поле на reversal-документе (SAP STGRD; D365/Odoo reason).
4. **Who/when** на обеих записях: SAP USNAM/CPUDT/CPUTM; NetSuite System Notes; Xero History & Notes.
   В erp-kaspi: существующий `audit_log` (проверить, что reversal пишет оба события).
5. **Именование** reversal-документа, производное от оригинала (Oracle `Reverses <name> <date> <time>`;
   Odoo `Reversal of: <ref>`; QBO суффикс «R») — для человеческого поиска.
6. **Иммутабельность**: оригинал не мутирует никогда (в erp-kaspi уже гарантировано
   `trg_operations_protect_posted_ledger`); reversal-документ после постинга — тоже.

**RECOMMENDED DEFAULT 🟢:** всё перечисленное — техническая область, система решает сама.
Отдельной audit-сущности не нужно (Oracle/SAP-прецедент: достаточно ссылок + audit_log).
**OWNER DECISION 🟢:** не требуется (информирование).

## D8. P&L impact: gross vs net

См. Q7. Дополнительно — **механика SAP negative posting**: reversal reason с флагом
«Negative postings allowed» постит реверс **той же стороной дебет/кредит с отрицательной суммой**:
обороты счёта уменьшаются, нетто ноль, вал не растёт. Без флага — противоположной стороной:
нетто ноль, вал растёт (иногда это нужно — аудит валовых потоков). Oracle: тот же выбор через
метод **Change Sign** (не раздувает) vs **Switch Dr/Cr** (раздувает, дефолт).

**Для erp-kaspi** (односторонняя модель, §27 sign semantics): аналога negative posting без
пересмотра знаковой семантики нет. Пара POSTED+POSTED нетится в ноль автоматически.
Вопрос — только представление (Q7).

**RECOMMENDED DEFAULT 🟡:** нетто (A из Q7).
**OWNER DECISION 🔴:** см. Q7.

## D9. Authorization: кто может реверсировать; approval chains

См. Q6. Дополнительно — **Oracle journal approval**: reversal batch идёт через стандартный
approval workflow (менеджер подтверждает до постинга); **D365**: approval chains на журналах;
**BC**: per-user posting windows как мягкая форма делегирования. Для малого бизнеса
(QBO/Xero/Odoo) — ролевая модель без цепочек.

**RECOMMENDED DEFAULT 🟡:** ACCOUNTANT + IP scope (текущее) — соответствует сегменту;
lock date — только ADMIN.
**OWNER DECISION 🔴:** см. Q6.

## D10. Idempotency: как предотвращаются дубли

**Практика — везде state-based, ни у кого нет API idempotency-key для реверса:**

| Система | Механизм |
|---|---|
| Oracle | Guard «posted and not yet reversed» (view-only Reversal Status) |
| SAP | Re-validation состояния **внутри транзакции постинга** + enqueue locks («not a reversing document or already reversed») |
| D365F | In-flight guard: «already in process… A separate reversal process can't be done» |
| BC | «An entry can only be reversed one time»; Canceled-check |
| NetSuite | Явные коды: **TRANS_ALREADY_VOIDED**, **TRANS_ALREADY_REFUNDED**, **TRANS_ALREADY_SETTLED** |
| Odoo/Xero/QBO | Валидации переходов состояний |

**erp-kaspi уже строже индустрии:** C-1 (commit 86f149b) ввёл `Idempotency-Key → import_fingerprint`
на POST /operations. Для reversal при модели C рекомендуется комбинация:
1. `SELECT … FOR UPDATE` оригинала в одной транзакции;
2. отказ, если уже реверсирован / сам является reversal (маркер, Q3);
3. вставка reversal + двусторонний штамп — атомарно;
4. idempotency-key на `POST /operations/{id}/reverse` (повтор с тем же ключом → вернуть
   существующий reversal, не создавать второй).

**RECOMMENDED DEFAULT 🟢:** всё перечисленное — техническая область.
**OWNER DECISION 🟢:** не требуется. (Владельцу на заметку: erp-kaspi здесь будет строже
Oracle/SAP/D365 — это плюс, а не отклонение.)

---

# PART C — СВОДНЫЙ ЧЕКЛИСТ ДЛЯ ВЛАДЕЛЬЦА (минимальный набор)

## 🔴 Только владелец (нет объективного ответа)

| # | Вопрос | Контекст |
|---|---|---|
| 1 | **Модель: A vs C** (B — сознательное отклонение от практики 7 систем + пересмотр ADR-007) | Q1 |
| 2 | **Бухгалтерская классификация компенсации**: новый kind vs иное; `affects_pnl` (рекомендовано true) | Q2 |
| 3 | **Частичный reversal**: нужен как бизнес-операция? (Да/Нет) | Q4 |
| 4 | **Авторизация**: достаточно ли ACCOUNTANT, или отдельное право / второе подтверждение? | Q6 |
| 5 | **P&L-представление**: нетто достаточно, или нужна отдельная видимость сторно? | Q7 |
| 6 | **Таксономия возвратов Kaspi**: refund как отдельная операция (связано с OQ-006) | D5 |

## 🟡 Владелец подтверждает дефолт (есть evidence-based рекомендация)

| # | Дефолт | Вопрос для подтверждения |
|---|---|---|
| 7 | Оригинал остаётся POSTED + маркер + `reversal_of_id` | Подтвердить «оригинал неизменен» (Q3) |
| 8 | `reversal_date` = параметр, дефолт сегодня; backdate — ? | Разрешить ли backdate в открытый прошлый период (D1) |
| 9 | Lock date (не периоды); меняет только ADMIN | Достаточно ли lock date (D6) |
| 10 | Void как отдельная операция — ? | Нужен ли void отдельно от reversal (D5) |

## 🟢 Система решает сама (владелец только информируется)

- Двусторонние ссылки + маркеры + формат именования reversal-документов (Q3, D7)
- Отдельное поле `reason`; запрет перезаписи comment (Q5, TD-019)
- Таксономия correction/reversal/void/refund в коде и UX (D5)
- State-guard + `SELECT FOR UPDATE` + idempotency-key на reverse endpoint (D10)
- Запрет реверса реверса; ошибочный reversal → третья ручная компенсация (D4)
- Lock-date механика и Closing-Date-Exception-отчёт (D6)
- Audit-события в `audit_log` для обеих сторон (D7)

---

# Источники (проверены 2026-10-03)

**Oracle (docs.oracle.com — официальные):**
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/revers01.htm — Generating Reversing Journal Batches
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/reverse.htm — Defining Reverse Journal Entries
- https://www.oracle.com/technetwork/fusion-apps/fincontrolandreporting-e22895-02-1514520.pdf — Fusion Financial Control and Reporting Guide (reversal methods, «Reversal journals cannot be reversed»)
- https://docs.oracle.com/applications/farel12/financialsop_gs/FAUGL/FAUGL.pdf — Fusion SOP: General Ledger (period close)
- https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N563543.html — NetSuite void
- https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N1469460.html — NetSuite reversing journals

**SAP (community.sap.com + операционные гайды; help.sap.com прямых страниц не отдал):**
- https://www.scribd.com/document/98728634/Sap-FB08-F-80-Tutorial-Document-Reversal — FB08 tutorial (reason codes 01/02/ZU, negative postings)
- https://community.sap.com/t5/application-development-discussions/payment-document-reversed/td-p/6586971 — XREVERSAL 1/2, STBLG/STJAH
- https://community.sap.com/t5/enterprise-resource-planning-q-a/reversal-reason/qaq-p/5787058 — reversal reasons
- https://community.sap.com/t5/enterprise-resource-planning-q-a/reversing-a-fi-document-related-to-sd/qaa-p/12210652 — reversal vs credit memo
- https://cg.sc.gov/sites/cg/files/Documents/Guidance%20and%20Forms%20for%20State%20Agencies/CGs%20Accounting%20Policies%20and%20Procedures/Accounting%20Guidance/How%20to%20Record%20Document%20Reversals%20Using%20SCEIS%20Transaction%20Code%20FB08.pdf — SCEIS FB08 guide (reason 02, «only reversed once»)

**Microsoft (learn.microsoft.com — официальные):**
- https://learn.microsoft.com/en-us/dynamics365/finance/general-ledger/reverse-journal-posting — D365F reverse journal
- https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions — D365F: reversal-of-reversal разрешён; in-flight guard; year-end close как второй замок
- https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice — BC Cancel (credit memo + Canceled)
- https://learn.microsoft.com/en-us/training/modules/correct-entries/4-reverse-journal-posting — BC «reverse only once»
- https://learn.microsoft.com/pt-pt/dynamics365/business-central/finance-how-reverse-journal-posting — BC reversing entry (same date/doc no.)
- https://learn.microsoft.com/is-is/dynamics365/business-central/localfunctionality/belgium/how-to-limit-the-posting-period — BC Allow Posting From/To

**Odoo:**
- https://www.odoo.com/documentation/18.0/applications/finance/accounting/reporting/year_end.html — lock dates
- https://www.odoo.com/documentation/12.0/applications/finance/accounting/others/adviser/reverse_entry.html — automatic reversal
- https://hibou.io/docs/accounting-3/journal-entries-90/ver/14-0-21 — Reverse Entry wizard (Specific / Journal Entry Date)
- https://www.cybrosys.com/blog/how-to-manage-reversal-of-the-journal-entries-in-odoo-17-storno-accounting — Storno

**QuickBooks / Xero:**
- https://quickbooks.intuit.com/learn-support/en-ca/help-article/list-management/void-delete-transactions-quickbooks-online/L5sZV8GYh_CA_en_CA — Void / Delete / Reverse; closing date + password (admin only)
- https://central.xero.com/0/article/Delete-or-void-a-sales-invoice — VOIDED; lock dates; credit note после lock date

**Ограничения исследования:** help.sap.com не отдал прямых страниц по FB08 (факты SAP подтверждены
community/гайдами — при подготовке ADR-016 перепроверить по SAP Help Portal); часть деталей
Odoo/QBO — из help-центров и партнёрских доков. Детальные находки: `docs/research/reversal_oracle.md`,
`docs/research/reversal_sap.md`, `docs/research/reversal_d365_odoo_others.md`.

---

*Конец decision package. Код не изменён. Следующий шаг после ответов владельца: ADR-016 → реализация.*
