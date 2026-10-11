# Halyk Bank / Freedom Bank — official integration options research

**Дата:** 2026-10-03
**Статус:** research only. Не код, не решение. Официальные источники только; то, что не найдено в официальных документах, зафиксировано как NOT_FOUND, а не выдумано.
**Контекст:** erp-kaspi сейчас работает с Kaspi Shop/Pay (см. `docs/KASPI_OQ003_DISCOVERY_2026-10-03.md`). Пользователь просил исследовать, как работают Halyk и Freedom для будущей интеграции.

---

## 1. Halyk Bank

### 1.1 ePay — публичный платёжный API (интернет-эквайринг) — VERIFIED

- **Developer portal:** https://epayment.kz/en-US/docs (официальный, Halyk Bank)
- **Проверено чтением:** https://epayment.kz/en-US/docs/mobile_sdk_documentation
- **Что покрывает:** приём онлайн-платежей картами (Visa, MasterCard, UnionPay, AmEx, JCB), Halyk QR, Apple Pay / Google Pay / Samsung Pay, MasterPass; P2P-переводы, AFT-платежи, OCT-переводы на карту и телефон; платёжный виджет; криптограммные API-платежи; генерация invoice-ссылок; подтверждение/отмена/возврат транзакций; управление сохранёнными картами.
- **Auth:** OAuth2-style — `TerminalID` (ID платёжного терминала мерчанта) + `ClientID` (ID мерчанта) + `ClientSecret` (секретный ключ) → токен. Credentials выдаются при регистрации мерчанта (требуется юрлицо/ИП).
- **Webhooks:** да — бэкенд мерчанта получает webhook со статусом платежа.
- **Sandbox:** есть (тестовые credentials упоминаются в документации портала).
- **Legacy domain:** `epay.kkb.kz` всё ещё активен.

**Важно для erp-kaspi:** это API *приёма платежей* (acquiring), а не API *выписок/выплат*. Через ePay нельзя получить выписку по расчётному счёту или инициировать исходящие платежи поставщикам/сотрудникам. Он полезен для связки «заказ → оплата», но не для bank reconciliation в смысле ERP-учёта.

### 1.2 Onlinebank для бизнеса — выписки (форматы выгрузки) — VERIFIED

Официальный документ Halyk «Functionality of transactions in Onlinebank»:
https://halykbank.eca.kz:6545/storage/app/media/2023/2023/12/ENG_Onlinebank_web_new_web_mobile.pdf

Форматы выгрузки выписок по текущему счёту:
- **PDF**, **XLS**
- **MT100**, **MT940**, **MT940v2**, **MT940 (UTF-8)**
- **1C**, **1C in UTF8**

То есть стандартный для KZ-банков набор: SWIFT MT940 (машиночитаемый) + 1C (прямая загрузка в 1С:Бухгалтерию) + XLS/PDF (ручные).

### 1.3 Business API для выписок/платежей/выплат — NOT_FOUND

Публичного официального API Halyk Bank для бизнеса (аналога open banking: получение выписок по API, инициирование платежей по API, выплаты по API) **в официальных источниках не найдено**. Нет публичного developer portal для Onlinebank; нет документированных REST-endpoint'ов для statements/payments. Это нормальная ситуация для KZ-банков: корпоративная интеграция идёт через закрытые каналы (банк-клиент с ЭЦП, host-to-host по договору), а не через публичный API.

### 1.4 Тарифы эквайринга — VERIFIED (контекст)

Официальный договор интернет-эквайринга: https://halykbank.kz/storage/app/media/2025/Documents/7/ENG_Internet_Acquiring.pdf — комиссии, батчинг (сверка итогов), отчёты по операциям (ежедневно/еженедельно/ежемесячно, email или в банке).

---

## 2. Freedom Bank (Казахстан)

### 2.1 Публичный developer API — NOT_FOUND

На официальном сайте https://bankffin.kz/ (проверено прямым открытием 2026-10-03) **нет раздела для разработчиков, API-документации или merchant API**. Поиск по официальным источникам (`bankffin.kz`, `ffin.kz`) не дал ни одной страницы API-контракта.

### 2.2 Freedom Pay (эквайринг) — PARTIALLY_VERIFIED

Существует сервис приёма платежей **Freedom Pay** (POS-терминалы и оплата на сайтах мерчантов; подтверждён UnionPay-аквайринг для карт UnionPay — пресс-релиз Freedom Holding Corp., NASDAQ:FRHC). Однако **публичной API-документации Freedom Pay не найдено** — подключение, судя по всему, идёт через менеджеров банка, а не self-service developer portal.

### 2.3 Выписки для бизнеса — NOT_FOUND

Официальных данных о форматах выгрузки выписок Freedom Business (1C/MT940/Excel) в открытых источниках не найдено. (Найденная инструкция FreedomOnline — это Таджикистан, `freedombank.tj`, другая юрисдикция и юрлицо — к KZ не применимо.)

### 2.4 Вывод по Freedom

Интеграция возможна только через **закрытые каналы по договору** (как и у большинства KZ-банков): ручная выгрузка выписок из Freedom Business + загрузка в ERP, либо host-to-host по согласованию с банком. Публичного API нет.

---

## 3. Сводная таблица статусов

| # | Пункт | Halyk Bank | Freedom Bank |
|---|---|---|---|
| 1 | Публичный платёжный API (acquiring) | **VERIFIED** — ePay, epayment.kz | **NOT_FOUND** (Freedom Pay существует, но публичных доков нет) |
| 2 | Business API: выписки по API | **NOT_FOUND** | **NOT_FOUND** |
| 3 | Business API: инициирование платежей | **NOT_FOUND** | **NOT_FOUND** |
| 4 | Business API: выплаты (payouts) | **NOT_FOUND** | **NOT_FOUND** |
| 5 | Форматы выгрузки выписок | **VERIFIED** — PDF, XLS, MT940 (v1/v2/UTF-8), MT100, 1C, 1C UTF-8 | **NOT_FOUND** в официальных источниках |
| 6 | Webhooks | **VERIFIED** — только ePay (статусы платежей) | **NOT_FOUND** |
| 7 | Sandbox | **VERIFIED** — только ePay | **NOT_FOUND** |
| 8 | Rate limits / error codes API | **NOT_FOUND** (не документированы) | **NOT_FOUND** |

---

## 4. Что это значит для erp-kaspi

### 4.1 Реалистичная архитектура интеграции (без выдуманных API)

Ни у Halyk, ни у Freedom **нет публичного API для выписок и платежей**. Поэтому реалистичный путь для erp-kaspi — **импорт банковских выписок из файлов**, а не live API:

1. **Halyk:** пользователь выгружает выписку из Onlinebank в формате **MT940** (или 1C/XLS) → загружает в erp-kaspi → парсинг → сверка с операциями.
2. **Freedom:** пользователь выгружает выписку из Freedom Business (формат уточнить у банка, вероятнее всего Excel/PDF) → загрузка в erp-kaspi.
3. **ePay (Halyk):** опционально позже — приём платежей через виджет + webhook для связки «заказ оплачен». Это отдельная интеграция уровня checkout, не reconciliation.

### 4.2 Что должен уметь generic bank-statement import в erp-kaspi

Минимальный набор (выведен из MT940-стандарта и KZ-практики):

- **Форматы входа:** MT940 (SWIFT, приоритет — машиночитаемый, есть у Halyk), CSV/XLS (ручной fallback), 1C (опционально, формат закрытый но стабильный).
- **Поля на операцию:** дата валютирования, сумма + знак (дебет/кредит), валюта, номер документа, БИК/ИИН контрагента, наименование контрагента, назначение платежа (с кодом назначения платежа КНП — критично для KZ-учёта), остаток.
- **Дедупликация:** по `(bank, account, doc_number, date, amount)` — аналог `import_fingerprint` в существующем ETL (`etl/import_excel.py` уже имеет механизм fingerprint; см. E-2).
- **Сопоставление:** statement line → `operations` (по сумме/дате/контрагенту) с ручным подтверждением несовпадений — тот же UX-паттерн, что Kaspi reconciliation (`v_kaspi_reconciliation`).
- **Provenance:** сырые файлы хранить в `raw_kaspi_payloads`-аналоге (таблица уже audited после A-2) — сейчас таблица называется `raw_kaspi_payloads`, для generic-подхода понадобится `raw_bank_statements` или переименование (миграция, не семантика).

### 4.3 Что НЕ делать

- Не проектировать «Halyk API client» для выписок — такого публичного API нет, это будет выдумка (нарушение правила KASPI_OQ_003).
- Не хардкодить парсинг под один банк — делать формат-ориентированный импорт (MT940 → parser, XLS → column-map как в `etl/column_map.example.yaml`).
- Не трогать Kaspi-модуль — банки добавляются как новые источники, не как замена.

---

## 5. Источники (все проверены 2026-10-03)

**Halyk (официальные):**
- https://epayment.kz/en-US/docs/mobile_sdk_documentation — ePay SDK docs (auth: TerminalID/ClientID/ClientSecret, webhook, invoiceId flow)
- https://halykbank.eca.kz:6545/storage/app/media/2023/2023/12/ENG_Onlinebank_web_new_web_mobile.pdf — форматы выписок Onlinebank (PDF/XLS/MT100/MT940/1C)
- https://halykbank.kz/storage/app/media/2025/Documents/7/ENG_Internet_Acquiring.pdf — договор интернет-эквайринга

**Freedom (официальные):**
- https://bankffin.kz/ — официальный сайт, раздела API нет (проверено прямым открытием)
- Пресс-релиз Freedom Holding Corp. (NASDAQ:FRHC) о Freedom Pay + UnionPay — подтверждает существование эквайринга, но не API

**Ограничения исследования:** детали ePay REST-endpoint'ов (точные пути, коды ошибок, rate limits) в публичной части документации фрагментарны; полный контракт выдаётся мерчанту при подключении (REQUIRES_REAL_ACCOUNT). По Freedom публичной документации нет вообще — всё через договор с банком.
