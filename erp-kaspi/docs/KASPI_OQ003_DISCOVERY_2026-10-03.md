# KASPI_OQ_003 — Official Kaspi API Documentation Discovery

- **Дата исследования:** 2026-10-03
- **Метод:** только официальные источники Kaspi (`guide.kaspi.kz`, `kaspipay.kz`, `kaspibank.kz`). Неофициальные блоги/форумы (habr.com, github community repos, apimonster, apipay.kz) использовались только как наводки и НЕ являются источниками контракта.
- **Статус:** OQ-003 остаётся **NOT_FOUND_IN_OFFICIAL_DOCS / PARTIALLY_VERIFIED по большинству пунктов**. Публичная документация покрывает только Seller API маркетплейса («Магазин на Kaspi.kz»); у Kaspi Pay нет публичного официального API.

---

## 1. Authentication method and base URLs — VERIFIED

- **Base URL:** `https://kaspi.kz/shop/api/v2`
- **Auth:** заголовок `X-Auth-Token` со статическим токеном. Токен генерирует **только руководитель компании** в веб-версии кабинета продавца: *Настройки → Токен API → «Сформировать»*. Токен не меняется со временем («в любой момент, ваш токен не будет меняться»).
- **Формат:** JSON:API — `Content-Type: application/vnd.api+json`.
- **Источники:**
  - https://guide.kaspi.kz/partner/ru/shop/api/general/q3196 — «Как получить токен авторизации в Магазине на Kaspi.kz?» — Обновлен 13.03.2025.
  - https://guide.kaspi.kz/partner/ru/shop/api/general/q3193 — «Как использовать API в Магазине на Kaspi.kz?» — Обновлен 13.03.2025.
  - https://guide.kaspi.kz/partner/ru/shop/api/orders/q3201 — примеры запросов с `X-Auth-Token` — Обновлен 11.06.2026.
- OAuth2 / API keys других типов, sandbox — **не задокументированы официально** (есть community-утверждение «нет тестовой среды», но это НЕ официальный источник).

## 2. Orders API (list, detail, status, entries/items) — VERIFIED (list, status; detail PARTIALLY)

### Список заказов — VERIFIED
- `GET https://kaspi.kz/shop/api/v2/orders?page[number]=0&page[size]=20&filter[orders][state]=NEW&filter[orders][creationDate][$ge]=…&filter[orders][creationDate][$le]=…&filter[orders][status]=APPROVED_BY_BANK&filter[orders][deliveryType]=PICKUP&filter[orders][signatureRequired]=false&include[orders]=user`
- Пагинация: `page[number]` (с 0), `page[size]` — **максимум 100 на странице**.
- Фильтры (официально): `filter[orders][state]` (NEW / SIGN_REQUIRED / PICKUP / DELIVERY / KASPI_DELIVERY / ARCHIVE), `filter[orders][creationDate][$ge|$le]` (мс), `filter[orders][status]`, `filter[orders][deliveryType]` (PICKUP/DELIVERY), `filter[orders][signatureRequired]`, `include[orders]=user`.
- Статус `meta.pageCount`, `meta.totalCount` в ответе.
- Источник: https://guide.kaspi.kz/partner/ru/shop/api/orders/q3201 — Обновлен 11.06.2026.

### Смена статуса заказа — VERIFIED
- `POST https://kaspi.kz/shop/api/v2/orders` (JSON:API, `type: "orders"`, `id`, `attributes.code`, `attributes.status`).
- Статусы: ACCEPTED_BY_MERCHANT (Принят), COMPLETED (Выдан покупателю), CANCELLED (Отменен), ARRIVED (Прибыл на склад), ARRIVED_BACKWARD (Возвращенный товар прибыл на склад), ASSEMBLE (Скомплектован).
- `cancellationReason` обязателен при CANCELLED: BUYER_CANCELLATION_BY_MERCHANT / BUYER_NOT_REACHABLE / MERCHANT_OUT_OF_STOCK. `cancellationComment` (макс. 1000 символов) для CANCELLING.
- Источник: https://guide.kaspi.kz/partner/ru/shop/api/orders/q3209 — Обновлен 25.09.2024.

### Состав заказа (entries) — PARTIALLY_VERIFIED
- Страница «удалить товары в заказе» (q3214, Обновлена 12.06.2024) прямо ссылается на официальный документ «получите информацию о товарах в заказе» и на `type: "orderentries"` (`GET /v2/orderentries/...`, `GET /v2/orderentries/{id}/product` упомянут в community-документации). Страница q3207 при прямом открытии вернула HTTP 500 от upstream — контракт полей entries по официальному источнику подтвердить не удалось.
- Дополнительно VERIFIED: `GET https://kaspi.kz/shop/api/v2/masterproducts/masterproductsID/merchantProduct` → `merchantproducts` (`code` — код товара в системе продавца, `name`, `manufacturer`). Источник: https://guide.kaspi.kz/partner/ru/shop/api/orders/q3208 — Обновлен 12.06.2024.

### Детализация одного заказа — PARTIALLY_VERIFIED
- `links.self: /v2/orders/orderID` и `related: /v2/orders/orderID/entries` присутствуют в официальных примерах JSON:API-ответов (q3201, q3209), но отдельной официальной страницы «GET /v2/orders/{id}» в выдачу не попало. Отмечать как недоказанную.

### Поля заказа (официально перечисленные, q3201)
`code`, `totalPrice` (тенге), `customer` (ФИО/телефон), `deliveryMode` (DELIVERY_LOCAL / DELIVERY_PICKUP + `isKaspiDelivery`), `paymentMode` (PAY_WITH_CREDIT / PREPAID), `recipient`, `signatureRequired`, `creditTerm`, `preOrder`, `state`, `creationDate`, `approvedByBankDate`, `plannedDeliveryDate`, `reservationDate` (все даты — миллисекунды), `status`, `deliveryCost`, `isImeiRequired`, `id`, `waybill`, `courierTransmissionPlanningDate`, `courierTransmissionDate`, `deliveryAddress`, `waybillNumber`, `category`, `deliveryCostForSeller`, `express`, `returnedToWarehouse`, `entries`, `user`.

## 3. Order cancellations and returns — VERIFIED (cancellations); returns NOT_FOUND_IN_OFFICIAL_DOCS

- Отмена заказа целиком: `status: CANCELLED` + `cancellationReason` (см. раздел 2). Отмена части товаров: `POST /api/v2/orderEntryCancelOperation` (`type: orderEntryCancelOperation`, attributes `notes`, `remainedQuantity`, `remainedWeight`, `reason`; ответ — `remainedWeight`, `remainedQuantity`, `status`). Источник: https://guide.kaspi.kz/partner/ru/shop/api/orders/q3214 — Обновлен 12.06.2024.
- **Возвраты (returns) через API — NOT_FOUND_IN_OFFICIAL_DOCS.** Статус `RETURNED`/`KASPI_DELIVERY_RETURN_REQUESTED` существует как фильтр, но API для оформления возврата в официальной документации не найден. Возвраты по Договору оформляются поручениями Партнера Банку (документ: https://guide.kaspi.kz/cdn/content/pay//product/documents/Dogovor-s-Kaspi-Magazinom.pdf, п. 4.1–4.4), не через публичный API.

## 4. Payment information per order — NOT_FOUND_IN_OFFICIAL_DOCS

- В официальной документации Seller API нет endpoint'а «платёж по заказу» (payment id, transaction ref, payment timestamps). Поле `paymentMode` (PAY_WITH_CREDIT / PREPAID) — это способ оплаты, не платёжная информация.
- Официально подтверждено правилами работы: «Деньги за реализованный товар после выдачи покупателю товара поступают на банковский счет партнера» (https://guide.kaspi.kz — Правила работы с Магазином на Kaspi.kz, п. 1.4/1.6) — это договорной факт, не API.

## 5. Payouts API (list, detail) — NOT_FOUND_IN_OFFICIAL_DOCS

- Официального публичного API для выплат продавцу не существует в открытых источниках Kaspi. Выплаты и взаиморасчёты регулируются Договором (сверка — ежемесячно до 7 числа месяца, следующего за отчётным; акт оказанных услуг — до 15 числа: https://guide.kaspi.kz/cdn/content/pay//product/documents/Dogovor-s-Kaspi-Magazinom.pdf).
- Данные о выплатах доступны продавцу через личный кабинет / приложение Kaspi Pay (официально: приложение Kaspi Pay, раздел «Магазин на Kaspi.kz»). Программного API не задокументировано.

## 6. Commissions and fees structure — PARTIALLY_VERIFIED (тарифы да, API-доступ к ним нет)

- Официальные страницы тарифов существуют: https://guide.kaspi.kz/partner/ru/shop/conditions/q3734 — «Почему в Магазине на Kaspi.kz поменялась комиссия за продажи?» — Обновлен 06.01.2026 (единая комиссия на основе среднего значения в категории; рассрочка 0-0-24 — исключение).
- Тарифы приёма оплаты Kaspi Red+: https://guide.kaspi.kz/partner/ru/red/conditions/q4586 (таблица по категориям).
- Комиссия при возвратах: по Договору (п. 4.2) комиссия за услуги по обработке данных возвращается, если товар возвращён покупателем; комиссия за Kaspi Доставку не возвращается. Документ: https://guide.kaspi.kz/cdn/content/pay//product/documents/Dogovor-s-Kaspi-Magazinom.pdf.
- **API для получения комиссий/тарифов — NOT_FOUND_IN_OFFICIAL_DOCS.**

## 7. Reconciliation data — NOT_FOUND_IN_OFFICIAL_DOCS (API); контрактная процедура VERIFIED

- API для сверки взаиморасчётов официально не задокументирован.
- Процедура сверки подтверждена официально: сверка — ежемесячно до 7 числа (Договор, раздел 8 / Договор Kaspi Pay — https://kaspipay.kz/documents/p3r.pdf, п. 3.7); порядок: перевод денег на счёт Партнёра в течение 4 банковских дней со дня операции (https://www.kaspibank.kz/news/file/41701004640.pdf — Приложение №4, п. 6.3).

## 8. Available fields per endpoint — PARTIALLY_VERIFIED

- Поля заказов — VERIFIED (перечень в q3201, см. раздел 2).
- Поля entries — NOT_FOUND_IN_OFFICIAL_DOCS (страница q3207 недоступна при прямом открытии; community-источники перечисляют `orderentries` с id/quantity/price, но это НЕ официальный источник).
- Поля статусных операций и `orderEntryCancelOperation` — VERIFIED (q3209, q3214).
- Товарные поля (`merchantproducts`) — VERIFIED (q3208).

## 9. Pagination, error codes, rate limits, idempotency — PARTIALLY_VERIFIED / NOT_FOUND_IN_OFFICIAL_DOCS

- **Pagination:** VERIFIED — `page[number]` (с 0), `page[size]`, максимум 100 записей/страница; ответ содержит `meta.pageCount`, `meta.totalCount` (q3201, Обновлен 11.06.2026).
- **Error codes:** NOT_FOUND_IN_OFFICIAL_DOCS. Документация упоминает только ожидаемый статус-код 200 для тестового запроса (q3193, Обновлен 13.03.2025). Справочника кодов ошибок нет.
- **Rate limits:** NOT_FOUND_IN_OFFICIAL_DOCS. Лимиты запросов официально не задокументированы.
- **Idempotency:** NOT_FOUND_IN_OFFICIAL_DOCS. Заголовки/ключи идемпотентности в официальной документации не упоминаются.
- **Webhooks:** NOT_FOUND_IN_OFFICIAL_DOCS (официально не упоминаются; community-источники подтверждают отсутствие нативной поддержки).

---

## Сводка: что остаётся заблокированным для KASPI_OQ_003

| # | Пункт чек-листа | Статус | Блокер |
|---|---|---|---|
| 1 | Auth + base URLs | VERIFIED | — |
| 2 | Orders list | VERIFIED | — |
| 3 | Orders detail / entries | PARTIALLY_VERIFIED | нужны поля `orderentries` и страница детализации заказа; страница q3207 недоступна (HTTP 500) |
| 4 | Status changes / cancellations | VERIFIED | — |
| 5 | Returns API | NOT_FOUND_IN_OFFICIAL_DOCS | REQUIRES_REAL_ACCOUNT и/или уточнение у поддержки Kaspi: оформляются ли возвраты только через кабинет |
| 6 | Payment info per order | NOT_FOUND_IN_OFFICIAL_DOCS | нет endpoint'а в официальной документации |
| 7 | Payouts API | NOT_FOUND_IN_OFFICIAL_DOCS | программный доступ к выплатам официально не задокументирован; только кабинет/приложение |
| 8 | Commissions via API | NOT_FOUND_IN_OFFICIAL_DOCS | тарифы есть на сайте (договорные), но API для них нет |
| 9 | Reconciliation via API | NOT_FOUND_IN_OFFICIAL_DOCS | только договорная процедура сверки (до 7 числа), без программного интерфейса |
| 10 | Fields per endpoint | PARTIALLY_VERIFIED | недоказан список полей `orderentries` |
| 11 | Pagination | VERIFIED | — |
| 12 | Error codes | NOT_FOUND_IN_OFFICIAL_DOCS | — |
| 13 | Rate limits | NOT_FOUND_IN_OFFICIAL_DOCS | — |
| 14 | Idempotency | NOT_FOUND_IN_OFFICIAL_DOCS | — |
| 15 | Kaspi Pay API (программные платежи) | NOT_FOUND_IN_OFFICIAL_DOCS | у Kaspi Pay нет публичного официального API; все Kaspi Pay-интеграции на рынке — через недокументированные/сторонние обёртки |

## Выводы

1. **Официально существует только Seller API маркетплейса** (`https://kaspi.kz/shop/api/v2`, JSON:API, `X-Auth-Token`). Он покрывает: список заказов, смену статусов (включая отмены), частичную отмену товаров, товарные данные. Документация фрагментарная (FAQ-стиль), без OpenAPI-спецификации, страницы редко обновляются (последние — 11.06.2026 для списка заказов, остальные 2024–2025).
2. **Ничего про payouts, reconciliation API, payment transactions, error codes, rate limits, idempotency, webhooks официально не опубликовано.** Выдумывать нельзя — эти пункты остаются KASPI_OQ_003 BLOCKED (REQUIRES_REAL_ACCOUNT / уточнение у Kaspi).
3. **Kaspi Pay не имеет публичного официального API для разработчиков.** Любые интеграции приёма платежей (QR/счета) — через недокументированные механизмы или сторонних агрегаторов; в официальный контракт их включать нельзя.
4. Неофициальные наводки (не источники контракта): habr.com/ru/articles/806447/ (опыт интеграции, нюансы фильтров), github.com/daul08/kaspishopclient, community MCP-исследования, apimonster.io коннектор. Устаревший протокол check/pay (habrastorage.org PDF) — утечка старого платёжного протокола, НЕ официальный источник.

**Рекомендация владельцу:** для закрытия OQ-003 нужен реальный аккаунт продавца (доступ к кабинету, токен) + письменное уточнение у поддержки Kaspi по пунктам 5–9, 12–15. До тех пор интеграция с Kaspi возможна только в режиме «чтение заказов + смена статусов по Seller API», а выплаты/комиссии/сверка — только через ручную выгрузку из кабинета (Excel) и договорные процедуры.
