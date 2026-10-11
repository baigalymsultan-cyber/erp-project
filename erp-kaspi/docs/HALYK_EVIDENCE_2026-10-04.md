# Halyk Bank — Official API Evidence (2026-10-04)

Sources: ONLY official Halyk documents (halykbank.kz, halykbank.eca.kz).
Blogs/forums/GitHub wrappers were NOT used as evidence.

**Headline update vs `docs/GAP_P1_BANKS_2026-10-04.md`:** the prior doc concluded
"no public Halyk Open API for business statements/payments (NOT_FOUND)".
This re-check found the **official Onlinebank Accession Agreement (Sept 2025)**
which DOES define an API-key integration with 7 service scopes. The correct
status is therefore **VERIFIED capability + ACCESS-GATED technical docs**,
not NOT_FOUND. There is still no *public* developer portal or endpoint spec.

## Capability table

| Capability | Official source URL | Status | Notes |
|---|---|---|---|
| Account statements + balances via API | https://halykbank.kz/storage/app/media/2025/Documents/11/%D0%9F%D1%80%D0%B8%D0%BB%D0%BE%D0%B6%D0%B5%D0%BD%D0%B8%D0%B5%201%20%D0%94%D0%BE%D0%B3%D0%BE%D0%B2%D0%BE%D1%80%20%D0%BF%D1%80%D0%B8%D1%81%D0%BE%D0%B5%D0%B4%D0%B8%D0%BD%D0%B5%D0%BD%D0%B8%D1%8F%20%D0%9E%D0%91_092025_DNA.pdf (Annex 10, "API KEY CREATION APPLICATION", service #5 "Account statements and balances service") | **VERIFIED** (capability) / **ACCESS-GATED** (tech spec) | Service scope exists in the official agreement; endpoint paths/fields are NOT public |
| Payments & transfers in KZT via API | Same document, Annex 10, service #2 | **VERIFIED** (capability) / **ACCESS-GATED** (tech spec) | — |
| Currency transfers via API | Same document, Annex 10, service #3 | **VERIFIED** (capability) / **ACCESS-GATED** (tech spec) | — |
| Currency conversion via API | Same document, Annex 10, service #4 | **VERIFIED** (capability) / **ACCESS-GATED** (tech spec) | — |
| Invoice service via API | Same document, Annex 10, service #1 | **VERIFIED** (capability) / **ACCESS-GATED** (tech spec) | — |
| Acquiring statement service via API | Same document, Annex 10, service #6 | **VERIFIED** (capability) / **ACCESS-GATED** (tech spec) | Acquiring (ePay/POS) statements, distinct from account statements |
| Payment order processing status via API | Same document, Annex 10, service #7 | **VERIFIED** (capability) / **ACCESS-GATED** (tech spec) | Status tracking of submitted payment orders |
| Web-service / API integration with accounting system | https://halykbank.kz/storage/app/media/2025/Documents/2/RUS_Onlinebank_Dogovor.pdf (service #27: "веб-сервис – интеграция с бухгалтерской/учетной системой Клиента посредством технологии API") | **VERIFIED** | Access via client's file certificate (EDS); integration built with client's own resources |
| 1C Direct Bank integration | Same document, service #25 ("1С Direct Bank – интеграция с «1С-Бухгалтерия» Клиента") | **VERIFIED** | 1C-native channel, separate from the API-key web-service |
| Auth method | Same Sept-2025 agreement, §2.1 "API key" definition + Annex 10 §5 | **VERIFIED** | API key = registration certificate (electronic document, EDS-signed) for integrating the client's accounting system; issued only in the authorized area of Onlinebank.kz ("My Account") by a user with first-signature rights; only the generating user may use/copy/delete it; documents created via API keep the standard EDS signing process |
| API integrations: statement loading, payment creation, cash-flow management | https://halykbank.eca.kz:6545/storage/app/media/Investoram/2025/9/RUS_HalykBank_ar2024.pdf (Annual Report 2024: "расширены API-интеграции, позволяющие загружать выписки, формировать платежи и управлять финансовыми потоками без необходимости ручного ввода данных") | **VERIFIED** | Strategic statement of capability, no technical detail |
| API connectivity + accounting-system integration (incl. 1C) | https://halykbank.eca.kz:6545/storage/app/media/O%20banke/2024/ESG/HalykBank_SR2024.pdf (Sustainability Report 2024) | **VERIFIED** | Strategic statement, no technical detail |
| Statement export MT940 / MT100 / 1C / XLS / PDF from Onlinebank | https://halykbank.eca.kz:6545/storage/app/media/2023/2023/12/ENG_Onlinebank_web_new_web_mobile.pdf ("Functionality of transactions in Onlinebank": Current account statement → PDF, XLS, MT100, MT940, MT940v2, MT940 UTF-8, 1C, 1C UTF8) | **VERIFIED** (re-confirmed) | Unchanged from prior doc; file-export path needs no API |
| ePay acquiring API (internet acquiring, NOT statements) | https://epayment.kz/en-US/docs/mobile_sdk_documentation (official ePay developer portal) | **VERIFIED** | Inbound card payments only; sandbox with public test credentials exists for ePay; base `https://epay.halykbank.kz` per portal docs |
| Public developer portal for the business/Open API | — | **NOT_FOUND** | No developer.halykbank.kz / api.halykbank.kz portal found; technical documentation is referenced in the agreement ("requests in accordance with the technical documentation") but distributed inside the authorized Onlinebank area only |
| Endpoint paths / request-response schemas for the business API | — | **ACCESS-GATED** | Exist per agreement, but only for onboarded Onlinebank business clients |
| Sandbox for the business API | — | **NOT_FOUND** | No official mention; ePay sandbox is acquiring-only |
| Production onboarding process | Same Sept-2025 agreement (accession via Application/Questionnaire + API Key Creation Application, Annex 10) | **ACCESS-GATED** | Requires: Halyk business account + Onlinebank connection + API Key Creation Application signed by authorized person; key generated in Onlinebank.kz "My Account" |

## Exact official wordings (evidence)

1. API key definition (§2.1, Sept-2025 agreement):
   "API key — A registration certificate in the form of an Electronic document
   issued to the Customer's authorized person without recording it on a Key
   carrier after the Authorized User signs the API key Creation Application in
   accordance with Annex 10 hereto, and used to accept and transfer the
   Customer's Electronic Documents when integrating the Customer's
   accounting/bookkeeping system or other system with the Onlinebank System."

2. API key usage (Annex 10, §5):
   "The API key is a secret key used only to send requests for creating payment
   documents and/or receiving information about accounts and documents."
   "The API key is required to send requests in accordance with the technical
   documentation."

3. Web-service (RUS agreement, service #27):
   "веб-сервис – интеграция с бухгалтерской/учетной системой Клиента
   посредством технологии API."

## Implication for ERP architecture

- The "user claim" (auto statement loading, payments, balance check via Halyk
  Open API) is **officially confirmed as a real capability** — but it is a
  **client-gated integration**, not a public API. No endpoint contract can be
  coded against until the business is onboarded and receives the technical
  documentation inside Onlinebank.
- Correct architecture posture: build the provider-adapter interface +
  normalized model + MT940 file import now (all bank-independent); leave the
  Halyk Onlinebank API adapter as **DEFERRED / ACCESS-GATED**, not
  EXTERNAL-BLOCKER (the capability exists — only the docs are gated).
- Do NOT invent endpoint paths, field names, auth headers, or rate limits.
