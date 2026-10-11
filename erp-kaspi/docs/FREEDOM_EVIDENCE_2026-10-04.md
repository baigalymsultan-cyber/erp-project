# Freedom Bank — Official Evidence (2026-10-04)

Agent: Freedom Bank official-sources research (subagent, 2026-10-04).
Prior: `docs/GAP_P1_BANKS_2026-10-04.md` (stated: NO public API found) — **confirmed again** after fresh search.

Rule applied: official bank site/docs/developer portals = proof; forums/socials/GitHub = direction pointers only, never contract proof.
No invented endpoints, fields, statuses, or auth flows.

## Capability evidence table

| Capability | Official source URL | Status | Notes |
|---|---|---|---|
| Statements API | — | NOT_FOUND | No statements endpoint in any official source. `site:bankffin.kz API` search returns only (a) a consent PDF mentioning the **NBK Open API platform register** (a regulatory framework, not a Freedom product API) and (b) a garbled PPTX mentioning "Business API" (content unrecoverable — not usable as evidence). |
| Transactions API | — | NOT_FOUND | Same as above. No transaction-list endpoint documented. |
| Balances API | — | NOT_FOUND | No balances endpoint documented. |
| Payments API (initiate) | — | NOT_FOUND | Payments are created inside FreedomOnline UI. No public payment-initiation API docs found. |
| Payment statuses API | — | NOT_FOUND | No status-query endpoint documented. |
| Developer portal | — | NOT_FOUND | No developer portal, API docs site, or sandbox found on bankffin.kz / freedombank.kz. |
| RRN availability (via API) | — | NOT_FOUND | No API; RRN would appear only inside statement exports (formats not officially named). |
| Commission data (via API) | — | NOT_FOUND | No commission endpoint. Commissions exist as tariff line items (see tariff PDF), not as an API. |
| Statement export — business clients | https://bankffin.kz/en/open-account (FAQ: "what's next?") | VERIFIED | Official: after account opening, client is redirected to **Internet Banking for Legal Entities "FreedomOnline"** at https://online.bankffin.kz/. "Certificates of account availability, turnover, absence of debt, and **statements in convenient formats** can be obtained online." Exact export formats (PDF/Excel/1C/MT940) are **not named** on the official page. |
| Statement export formats | — (official page silent) | NOT_FOUND (official) / pointer (community) | Community pointer only (moysklad.ru integration docs, VERIFIED COMMUNITY): Freedom Bank statements importable as **1C-format (.txt)** manual files — implies 1C export exists in the online bank, but this is NOT official proof of format. Related: FreedomOnline Tajikistan user guide (official freedombank.tj domain, but **Tajik entity, not KZ**) documents PDF+Excel statement export — direction pointer only for KZ. |
| Acquiring API (Freedom Pay / internet acquiring) | https://bankffin.kz/en/open-account (menu lists "Internet acquiring") | NOT_FOUND (docs) | Acquiring products exist (POS, mobile, internet acquiring; UnionPay acquiring press release 2025-09-03). No public merchant API documentation found. |
| Tariff/commission reference | https://bankffin.kz/docs/ib_tariff_freedom_en.pdf?v=2.5 | VERIFIED | Official tariff catalog for FreedomOnline legal-entity packages (payments, FX, cash). Usable as **reference config only** (fee amounts), not as transaction data source. No API or statement-format info inside. |
| 1C integration | — | NOT_FOUND (official) | No official 1C:DirectBank or client-bank file-format documentation found on bankffin.kz. |

## EXTERNAL-BLOCKER

**EB-FREEDOM-1 — No official Freedom Bank API of any kind (statements, transactions, balances, payments, statuses, RRN, commissions, webhooks, sandbox).**

- Search paths tried: `site:bankffin.kz API`; EN/RU web search for Freedom Bank Open API / developer portal / acquiring API docs / statement export formats; official tariff PDF (ib_tariff_freedom_en.pdf); official account-opening page (bankffin.kz/en/open-account); business internet-banking references.
- Evidence of absence is consistent with the 2026-10-04 banks report (G-BANK): Freedom = NO public API.
- The only "API" mentions on the official domain refer to the National Bank of Kazakhstan's Open API platform register (regulatory), not a Freedom product.

## What CAN be built without the blocker (confirmed channel)

- **Manual statement import** from FreedomOnline (https://online.bankffin.kz/): business client downloads the statement in whatever "convenient formats" the UI offers (formats to be confirmed empirically from a real account — 1C .txt is the community-reported format), then imports through the generic bank-statement pipeline (`raw_bank_statements` → provider adapter → normalized transaction → reconciliation → financial operation) per `docs/BANK_ADAPTER_DESIGN_2026-10-04.md`.
- **No Freedom-specific adapter code should be written** until at least one real export file format is confirmed from the official online bank; the adapter must be format-driven, not invented.
- Tariff PDF can seed reference fee amounts for `EXPENSE_BANK_FEE` classification review (config, not transaction source).

## Classification summary

- VERIFIED OFFICIAL: FreedomOnline business internet banking with online statements (formats unnamed).
- NOT_FOUND officially: every API surface (statements, transactions, balances, payments, statuses, RRN, commissions, developer portal, sandbox, acquiring API docs, 1C integration docs).
- VERIFIED COMMUNITY (not contract proof): 1C .txt manual statement import support.
- INFERENCE: statement export formats likely include PDF/Excel/1C (standard KZ practice + community pointer), but **not confirmed** — must be verified against a real FreedomOnline export before adapter implementation.
