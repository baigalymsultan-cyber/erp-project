# Oracle Financials: как разворачиваются проводки (GL / AP)

Исследование, 2026-10-03. Источники — официальная документация Oracle (docs.oracle.com) и официальные гайды Oracle на oracle.com.
Задача: ответить на 7 вопросов владельца erp-kaspi для решения «compensating-journal vs status-flip reversal».
Статус-блокеры (REVERSAL_Q1_Q7, TD-020 и др.) этим исследованием не разрешаются — это только внешняя референсная практика.

---

## 1. Как Oracle разворачивает запощенную (posted) проводку

**Наблюдаемое поведение:**
- Oracle НИКОГДА не меняет исходную posted-проводку. Реверс = создание НОВЫХ компенсирующих проводок через отдельную операцию «Reverse».
- В EBS GL: «You can generate reversing entries from the Enter Journals window… If you reverse a journal batch, General Ledger creates a reversing journal entry for each journal entry in your batch. Note that this also generates a separate reversal batch for each reversed journal. General Ledger automatically names the reversal batch `Reverses [Original Journal Entry Name] [Date] [Time]`».
- Реверсирующая проводка создаётся сначала как UNPOSTED, затем её нужно отдельно запостить: «Choose Reverse Journal… Post the reversing journal batch».
- Два метода создания компенсирующих строк:
  - **Switch Dr/Cr** — дебет и кредит меняются местами (часто для аккруалов).
  - **Change Sign** — сумма ставится в тот же столбец, но со сменой знака (часто для исправления ошибок ввода). По умолчанию в Fusion: Switch Dr/Cr.
- В Fusion то же самое: «You can reverse journals manually by selecting a reversal action in the user interface, or you can reverse journals automatically by running a process». Условия допустимости (ниже) включают «The journal is posted and not yet reversed».
- Исходная проводка получает признак «Reversed»: в UI поле **Reversal Status** — view-only, значения «not reversed / reversed». После реверса статус исходного журнала меняется на Reversed (это блокирует повторный реверс).

**Источники:**
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/revers01.htm (Generating Reversing Journal Batches, Oracle General Ledger Users' Guide)
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/reverse.htm (Defining Reverse Journal Entries)
- https://docs.oracle.com/cd/G49759_01/trans/G46389-01/using-general-ledger.pdf (Oracle Fusion Cloud Financials — Using General Ledger, «When does a journal become reversible?»)
- https://docs.oracle.com/cd/G38197_01/trans/G34378-01/using-general-ledger.pdf (то же, другая версия)

**Какие вопросы владельца закрывает:** модель реверса — однозначно compensating journals (не status-flip); исходник не трогается, маркируется Reversed. Это прямой ответ на вопрос «reversal model» в REVERSAL_Q1_Q7 — Oracle-паттерн: оригинальный POSTED остаётся в P&L закрытого периода, нетто-эффект даёт новая компенсирующая проводка.

---

## 2. Дата реверса: тот же период или следующий

**Наблюдаемое поведение:**
- Реверс всегда идёт в заданный **reversal period** (параметр операции), а не «назад в период исходника».
- Варианты reversal period (EBS): Same Period (тот же, что у исходника), Next Period/Next Non-Adjusting Period (следующий после исходного), Next Day / First Day / Last Day (для average balance ledgers — дата = первый/следующий/последний день периода реверса). В Fusion journal reversal criteria sets: Same Period, Next Period, Next Nonadjusting Period (+ Next Day, Same Day для average balance ledgers); Reversal Date: First Day, Next Day, Last Day — только для average balance ledgers.
- Жёсткое правило: **reversal period должен быть open или future-enterable** (EBS: «it may not be reversible for several reasons, such as the reversal period or effective date is closed»; Fusion: «The reversal period is open or future enterable»).
- **В закрытый период реверс невозможен.** Типовой сценарий Oracle: аккруал реверсится в начале следующего открытого периода (first day of next period).
- «Reversal period is the same or after the accounting period» (валидация при вводе журнала в Fusion).

**Источники:**
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/revers01.htm
- https://docs.oracle.com/cd/G49759_01/trans/G46389-01/using-general-ledger.pdf («Settings That Affect Journal Reversals»: «Reversal period is open or future enterable»)
- https://docs.oracle.com/cd/E79783_03/current/acrobat/122glug.pdf (таблица Effective Date Rule × Reversal Date, стр. 1-209/1-210)
- https://docs.oracle.com/applications/farel12/financialsop_gs/FAUGL/FAUGL.pdf

**Какие вопросы владельца закрывает:** «reversal date» — в Oracle это не дата исходной операции и не «сейчас» произвольно, а явный reversal period (same/next), обязанный быть открытым/future-enterable. Закрытые периоды недотрагиваемы. Если erp-kaspi примет compensating-модель, дату компенсирующей проводки следует делать параметром с валидацией «период открыт» — а не копировать дату оригинала.

---

## 3. Реверс реверса (reversal of a reversal)

**Наблюдаемое поведение:**
- **Запрещён.** В Fusion таблица критериев «When does a journal become reversible?»: «The journal isn't a reversal journal. **Note: Reversal journals can't be reversed**» — относится и к manual, и к automatic reversal.
- В EBS то же: «Settings That Affect Journal Reversals» — «Not a reversal journal. Reversal journals cannot be reversed in Oracle Fusion General Ledger».
- Для реплицированных валютных журналов (reporting currency): они реверсятся вместе с исходным, а не отдельной операцией.
- Воркэраунд в документации не описан. Теоретически можно вручную ввести зеркальный журнал, но штатный Reverse на reversal-журнал не работает.

**Источники:**
- https://docs.oracle.com/cd/G49759_01/trans/G46389-01/using-general-ledger.pdf
- https://docs.oracle.com/cd/E25178_01/fusionapps.1111/e20374/F484499AN19B45.htm (Fusion Accounting Hub Implementation Guide: «Not a reversal journal. Reversal journals cannot be reversed»)

**Какие вопросы владельца закрывает:** «reversal-of-reversal» — Oracle запрещает реверсировать реверс штатной операцией. Это ограничение служит защитой от цепочек компенсирующих проводок и путаницы в аудите. Если erp-kaspi нужен такой сценарий — потребуется ручная компенсирующая операция, а не обратный вызов reverse_operation().

---

## 4. Аудит-трейл реверса

**Наблюдаемое поведение:**
- Связь «оригинал → реверс» кодируется в имени reversal batch: `Reverses [Original Journal Entry Name] [Date] [Time]` — по имени можно найти исходник.
- У журнала есть поле **Reversal Status** (view-only: not reversed / reversed).
- При AutoReverse процесс автоматически создаёт **AutoReverse Execution report**: печатает journal name, reversal period для каждого успешно реверсированного журнала и факт отправки reversal-журнала на постинг. Также есть Automatic Posting Execution Report и Posting Execution Report после постинга.
- Реверсированная проводка проходит обычный постинг (Posted Journals Report, General Ledger и Account Analysis reports).
- Специального «журнала аудита реверсов» в GL в найденных документах нет; аудит строится из связки: исходный журнал (статус Reversed) + именованный reversal batch + стандартные отчёты по проводкам.

**Источники:**
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/revers01.htm (именование `Reverses ...`)
- https://docs.oracle.com/applications/farel12/financialsop_gs/FAUGL/FAUGL.pdf («General Ledger automatically creates the AutoReverse Execution report when the AutoReverse process completes successfully. The report prints the journal name and reversal period for each journal that is successfully reversed»)
- https://docs.oracle.com/cd/E18727_01/doc.121/e13627/T312864T314184.htm (статусы журналов, в т.ч. Reversed для consolidation)

**Какие вопросы владельца закрывает:** «audit trail» — Oracle не ведёт отдельную audit-таблицу реверсов; связь обеспечивается именованием batch и статусом оригинала + отчётами AutoReverse Execution / Posting Execution. Для erp-kaspi это аргумент за хранение явной ссылки `related_operation_id` + аудит-событий в существующем audit log (уже есть по проекту), а не отдельной сущности.

---

## 5. Correction vs Reversal vs Void vs Cancellation

**Наблюдаемое поведение (официальная таксономия Oracle):**
- **Correction (исправление до постинга):** «Edit unposted journals to change or correct information, including the batch period and the journal currency». Незапощенные журналы можно править и удалять. После постинга правка невозможна — только реверс.
- **Reversal (после постинга):** создание компенсирующих проводок (см. п.1). В AR: если invoice «paid, posted, printed, or has had a receipt applied» — неполное (incomplete) невозможно, «you must reverse the transaction (delete it, credit it, or change the transaction type…)».
- **Void (аннулирование платёжного документа, AP):** «When you void a payment, Payables automatically reverses the accounting and payment records… and so the status of the paid invoices is reset to Unpaid». Void — отдельная терминальная операция, **её нельзя откатить**: «Since you cannot reverse a void on a void payment» — перед void требуется убедиться, что платёж не может быть погашен (stop payment в банке).
- **Cancel (отмена счёта/инвойса):** Invoice Action = Cancel при void платежа «cancel all related invoices and reset their Amounts to zero». В Fusion SLA предопределены события: **Invoice Canceled, Credit Memo Canceled, Debit Memo Canceled, Payment Canceled, Prepayment Canceled, Refund Canceled** — т.е. cancel = бизнес-событие в субледжере, которое через Subledger Accounting генерирует сторнирующую проводку, а не «удаление записи».
- Дополнительно: Unapply Prepayments «automatically creates a reversal distribution»; withholding tax при void платежа → автоматически создаётся negative (reversing) invoice.

**Источники:**
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/gl/jeover.htm (The General Ledger Accounting Cycle: «Edit unposted journals to change or correct information»)
- https://docs.oracle.com/cd/E18727-01/doc.121/e12797.pdf (Oracle Payables User's Guide: Voiding Payments; «you cannot reverse a void on a void payment»)
- https://docs.oracle.com/cd/A85683_03/acrobat/115apug.pdf (Invoice Action: Cancel → «cancel all related invoices and reset their Amounts to zero»)
- https://www.oracle.com/technetwork/fusion-apps/paycash-e22897-02-1514523.pdf (Payables Event Classes and Types: Invoice Canceled и др.)
- https://docs.oracle.com/cd/A60725_05/html/comnls/us/ap/prepmt07.htm (Unapplying Prepayments: «Payables automatically creates a reversal distribution»)

**Какие вопросы владельца закрывает:** «correction vs reversal vs refund vs void» — Oracle разводит четыре понятия: correction = правка до постинга; reversal = компенсирующие проводки после постинга; void = необратимая отмена платёжного документа с авто-сторнированием учёта; cancel = бизнес-событие отмены документа, порождающее сторнирующие события SLA. Void нельзя реверсировать вообще. Это прямой материал для ADR-016 и терминологии в decision memo.

---

## 6. Защита P&L закрытых периодов

**Наблюдаемое поведение:**
- Период закрывается в конце цикла: «When you finalize the activity for an accounting period, close the period to prevent the entry or posting of additional journal entries». После закрытия **постинг в этот период невозможен** — это и есть защита исторических P&L-цифр.
- Порядок закрытия в EBS: сначала закрываются субледжеры (AP/AR/…), потом GL. «Close the period for each subledger. This prevents future subledger transactions from being posted to General Ledger in the same period».
- Постинг в открытый предшествующий период возможен («as long as the prior period is open» — GL автоматически пересчитывает начальные остатки последующих периодов; постинг в прошлый год корректирует retained earnings). Но в закрытый — нет: реверс обязан идти в open/future-enterable период.
- Year-end: отдельные balance sheet closing journals (аккруалы обнуляются, остатки переносятся); closing journals помечаются для реверса в следующем периоде.
- ⚠️ Про «permanently closed periods can never be reopened» — встречено во вторичном источнике (slideshare, не docs.oracle.com), официальной формулировки в найденных документах нет. В Fusion периоды имеют статусы Open / Closed / Future-Enterable, закрытый можно переоткрыть (reopen) при наличии прав, но постинг в закрытый период блокируется.

**Источники:**
- https://docs.oracle.com/applications/farel12/financialsop_gs/FAUGL/FAUGL.pdf («close the period to prevent the entry or posting of additional journal entries»; «posted to a current or prior accounting period… as long as the prior period is open»)
- https://www.scribd.com/document/57841785/Year-End-Closing-GL-1-1 (порядок закрытия субледжеров → GL — вторичный источник, пометить как таковой)

**Какие вопросы владельца закрывает:** «historical P&L preservation» — Oracle защищает историю именно закрытием периодов (не запретом реверса как такового): реверс всегда пишется в открытый период, закрытые периоды иммутабельны. Для erp-kaspi это аргумент за модель «периоды со статусом open/closed + валидация даты компенсирующей проводки», а не «запрет реверса старых операций».

---

## 7. Конкурентность / идемпотентность: защита от двойного реверса

**Наблюдаемое поведение:**
- Основной механизм — проверка **Reversal Status** перед операцией: реверс допустим только для журнала «posted and not yet reversed»; повторная попытка реверсировать уже реверсированный журнал не проходит критерий допустимости.
- Отдельно запрещены: реверс reversal-журналов, реверс журналов с замороженным source (SLA), реверс реплицированных валютных журналов (реверсятся вместе с исходником).
- ⚠️ **Не подтверждено в найденных документах:** явный механизм идемпотентности (idempotency-key, уникальный констрейнт на (original_journal_id), блокировки строк при конкурентном реверсе). Документация описывает только проверку статуса как предусловие операции. Про поведение при двух одновременных Reverse-запросах к одному журналу официальной информации не найдено.

**Источники:**
- https://docs.oracle.com/cd/G49759_01/trans/G46389-01/using-general-ledger.pdf («The journal is posted and not yet reversed», «The journal isn't a reversal journal»)
- https://docs.oracle.com/applications/farel12/financialsop_gs/FAUGL/FAUGL.pdf («Posted but not yet reversed»; «Not a reversal journal»)

**Какие вопросы владельца закрывает:** «idempotency/concurrency» — Oracle полагается на флаг состояния (Reversed) как предусловие, что эквивалентно application-level guard, но не заменяет DB-констрейнт. Для erp-kaspi это подтверждает уже принятое направление (C-1: Idempotency-Key + uq_operations_import_fingerprint) — но показывает, что Oracle не даёт готового рецепта идемпотентности API; флаг статуса + уникальный ключ остаются проектным решением.

---

## Сводная таблица: Oracle-паттерн vs вопросы владельца

| Вопрос владельца (REVERSAL_Q1_Q7) | Ответ Oracle |
|---|---|
| Модель реверса | Только compensating journals; оригинал неизменен, помечается Reversed |
| Дата реверса | Параметр reversal period: same/next/nonadjusting; обязан быть open/future-enterable; в закрытый период нельзя |
| Реверс реверса | Запрещён штатной операцией |
| Аудит-трейл | Связь через имя batch `Reverses <name> <date> <time>` + статус Reversed + отчёты AutoReverse Execution / Posting Execution |
| Correction vs reversal vs void vs cancel | Correction — правка до постинга; reversal — компенсация после; void — необратимая отмена платежа с авто-сторно; cancel — бизнес-событие, генерирующее сторнирующие SLA-события |
| Сохранение истории P&L | Закрытие периодов блокирует постинг в них; реверс всегда в открытый период |
| Идемпотентность | Guard по статусу «posted and not yet reversed»; явного idempotency-механизма в док-ции нет (не подтверждено) |
