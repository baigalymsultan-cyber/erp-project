# Kaspi API — Official Evidence Table (fresh re-check 2026-10-04)

**Method:** web search of official Kaspi sources only (`guide.kaspi.kz` — Kaspi Гид для Партнёров,
`kaspibank.kz` — official partner agreement appendices). Community/GitHub/API aggregators used
only as pointers, never as contract proof. KASPI_OQ_003 freeze respected: no invented endpoints.
Consistent with `docs/KASPI_OQ003_DISCOVERY_2026-10-03.md` (2026-10-03 discovery, still valid).

**Evidence levels used:** `VERIFIED OFFICIAL` (official Kaspi page/PDF) · `VERIFIED COMMUNITY`
(consistent community research, not contract proof) · `NOT_FOUND` (no official source located).

---

| # | Capability | Official Source URL | Status | Notes |
|---|-----------|---------------------|--------|-------|
| 1 | Payout items / payout registry API | — | **NOT_FOUND** | No official Kaspi endpoint for payout items, payout registry, or payout listing found in Shop API docs or partner guide. Community research (2026-09/10) independently confirms only orders/products surface exists. **EXTERNAL-BLOCKER**: `KASPI-002`. What CAN be built: `POST /kaspi/payouts/{id}/items` manual-entry API (data from Kaspi Pay app export), `raw_kaspi_payloads` provenance, adapter interface stub marked DEFERRED. |
| 2 | Commission API | — | **NOT_FOUND** | No commission API endpoint. Official alternative data source — see rows 5–6. **EXTERNAL-BLOCKER**: `KASPI-003`. What CAN be built: `post_payout_commission()` service posting `EXPENSE_KASPI_FEE` from manually entered / statement-imported commission amounts (idempotency fingerprint `kaspi-commission:{payout_id}`); commission tariff table (official, row 6) as reference config — NOT as auto-fetch. |
| 3 | Settlement API | — | **NOT_FOUND** | No official settlement/bank-statement API. Official mechanism: account statement export via Kaspi Pay (row 5). **EXTERNAL-BLOCKER**. What CAN be built: normalized statement import (CSV/XLS/PDF-parsed statement lines → `raw_bank_statements` staging → reconciliation), same adapter interface as Halyk/Freedom. |
| 4 | Payment reconciliation API | — | **NOT_FOUND** | No official reconciliation API. Partially related official info: partner agreement Appendix 2 lists «Информация по результатам сверок по услугам и продажам на Kaspi.kz» as an informational e-service inside Kaspi Pay (not an API — a screen/export). **EXTERNAL-BLOCKER**. What CAN be built: internal `reconcile_payout_full()` 8-class classifier (already exists) fed by statement-import lines + Shop API orders. |
| 5 | Kaspi Pay statement export (official mechanism) | https://www.kaspibank.kz/news/file/188108644604.pdf (official partner agreement, Appendix 2, §3.1.2) | **VERIFIED OFFICIAL** | Official text: «Экспорт из Kaspi Pay платежных документов, Выписок по Счету (-ам), выписок по POS-терминалам в форматах, определяемых на Kaspi Pay». Kaspi determines the formats; statements carry Kaspi's electronic stamp (not a legal original — originals only in branch offices). Storage of all Kaspi Pay user actions: 5 years electronic. This is the **official import path** for settlement data: manual/automated export → parse → normalize → reconcile. |
| 6 | Commission visible in Kaspi Pay statement | https://guide.kaspi.kz/partner/ru/red/returns/q369 («Как зачисляются деньги от продаж с Kaspi Pay?», updated 13.03.2025) | **VERIFIED OFFICIAL** | Official text: «Деньги за продажи поступают на Счет Kaspi Pay онлайн после каждой принятой оплаты **за вычетом комиссии**. В выписке по текущему счету поступления за принятую оплату и **списанные комиссии отображаются двумя общими суммами**». Per-operation detail visible in Kaspi Pay mobile app: «на главной странице в разделе "Продажи за сегодня" выберите необходимый период и точку продаж». → Commission data source = statement export (aggregate) + app detail (per-operation). No API. |
| 7 | Commission tariff tables (official rates) | https://guide.kaspi.kz/partner/ru/shop/conditions/q3734 (shop commissions, updated 06.01.2026); https://guide.kaspi.kz/partner/ru/kredit/conditions/q3769 ; https://guide.kaspi.kz/partner/ru/red/conditions/q4586 | **VERIFIED OFFICIAL** | Official per-category commission % with/without VAT (e.g. Мебель 6.4%/7.3%, Одежда 9.1%/10.4%, Прочее 11.3%/13.0%). Published tables, NOT an API. Usable as reference config for expected-commission cross-checks; must not be treated as per-transaction truth (actual = statement). |
| 8 | Shop API v2 — orders surface | https://kaspi.kz/shop/api/v2 (base); docs at https://guide.kaspi.kz/partner/ru/shop/api/ (guide index; API pages q3193/q3196/q3201/q3208/q3209) | **VERIFIED OFFICIAL** | Marketplace Seller API only. Auth: `X-Auth-Token` static token (director generates in seller cabinet). Endpoints: `GET /v2/orders` (pagination max 100/page, filters), `POST /v2/orders` (status update), orderentries, masterproducts/merchantproducts, pointsofsale, cities, merchantreviews. JSON:API format. Confirmed by 2026-10-03 discovery; re-confirmed via community research 2026-09/10 (consistent, no contradictions). |
| 9 | Kaspi Pay public merchant API | — | **NOT_FOUND** | Consistently confirmed absent across community research (2026-09/10): «Kaspi Pay has NO official public API — only third-party wrappers (ApiPay.kz, AiPay.kz)». Kaspi Pay refund protocol PDF found on habrastorage is a **leaked partner-integration doc** (2021, `returnApi/Refund/RefundRequest`), not a public official API — treat as `VERIFIED COMMUNITY` at best, not a contract. **EXTERNAL-BLOCKER** for any automated Kaspi Pay integration. |
| 10 | Webhooks / rate limits / error codes (Shop API) | — | **NOT_FOUND** | Not documented officially. Community: no native webhooks (third-party polling via ApiMonster.io). Do not invent. |
| 11 | Chain: Sale → Settlement → Commission → Account → Reconciliation — officially available data | rows 5, 6, 8 | **VERIFIED OFFICIAL (partial chain)** | What IS available: **Sale** — Shop API orders (row 8); **Settlement** — Kaspi Pay account statement export (row 5), sales arrive net of commission online per accepted payment; **Commission** — statement aggregate lines + per-op app detail + official tariff tables (rows 6–7); **Account** — statement per account; **Reconciliation** — internal only (classifier exists in repo), fed by statement import + Shop API. Missing (NOT_FOUND): any API for payouts, commissions, settlements, reconciliation, webhooks. |

---

## EXTERNAL-BLOCKER summary

| Blocker | Missing capability | Evidence | What can be built without it |
|---------|-------------------|----------|------------------------------|
| EB-KASPI-1 (KASPI-002) | Payout items / payout registry API | NOT_FOUND, row 1 | Manual `POST /kaspi/payouts/{id}/items` entry API; statement-import auto-suggest; adapter interface stub DEFERRED |
| EB-KASPI-2 (KASPI-003) | Commission API | NOT_FOUND, row 2 | `post_payout_commission()` from statement/manual input; tariff-table cross-check config |
| EB-KASPI-3 (F-ETL-003) | Kaspi Shop→ERP auto-sync API surface beyond orders; Kaspi Pay API entirely | NOT_FOUND, rows 8–9 | `ShopOrderAdapter` for the VERIFIED orders surface (owner token needed: OQ-KASPI-001); statement-file import pipeline for settlements |
| EB-KASPI-4 | Settlement / reconciliation API | NOT_FOUND, rows 3–4 | Normalized statement model (`raw_bank_statements` staging → provider adapter → normalized transaction → reconciliation → financial operation); Kaspi Pay statement parser (formats defined by Kaspi — parse on receipt, do not hardcode layout assumptions) |

## Architecture note (per parent task)

Official money flow per Kaspi's own docs: **Kaspi Sale** (Shop API order) → **Kaspi Settlement** (net-of-commission credit to Kaspi Pay account, visible in statement export) → **Commission** (separate aggregate line in the same statement + per-op detail in app) → **Bank/Kaspi account** (statement per account) → **Reconciliation** (internal classifier, already in repo) → **Financial Operation** (POSTED, accrual_date = sale/economic date, operation_date = settlement date).
Do NOT merge these entities: commission is a separate `EXPENSE_KASPI_FEE` operation, not a netted sale amount; settlement date ≠ sale date (accrual vs cash basis).

## No code changed (per task DONE condition)
