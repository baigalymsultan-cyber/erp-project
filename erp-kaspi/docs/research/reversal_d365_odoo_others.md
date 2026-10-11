# Posted-transaction reversal patterns: D365, Odoo, NetSuite, QuickBooks Online, Xero

**Research purpose:** feeds the erp-kaspi decision memo (owner open questions Q1–Q7:
reversal model, reversal date, reversal-of-reversal, audit trail, correction vs
reversal vs refund vs void, historical P&L preservation, idempotency/concurrency).
**Research only — no code changes.** Date of research: 2026-10-03.

Each claim carries the source URL where it was found. Anything not found in an
official/credible source is marked **not confirmed** rather than invented.

---

## 1. Microsoft Dynamics 365 — Business Central

### Q1. Reversal model — hybrid (compensating journal + status flag + new corrective document)

- **Reverse Transaction (journal postings):** compensating entry. Reversing a
  transaction "doesn't remove the entries from the FA ledger but automatically
  creates the same entry with the opposite sign."
  Source: https://learn.microsoft.com/en-us/training/modules/correct-entries/4-reverse-journal-postings
- **Manual reversing entry:** "A reverse entry is the same as the original entry,
  but has an opposite sign in the **Amount** field."
  Source: https://learn.microsoft.com/pt-pt/dynamics365/business-central/finance-how-reverse-journal-posting
- **Cancel posted sales invoice:** hybrid — auto-creates **and posts a corrective
  credit memo** (a new compensating document applied against all G/L and inventory
  ledger entries from the original invoice), and sets the **Canceled** flag to
  **Yes** on the original posted invoice (status-flip).
  Source: https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice
- **Correct (Create Corrective Credit Memo):** creates a corrective credit memo as
  an editable draft; user edits and posts it. "This application reverses the
  posted sales invoice in your financial records and leaves the corrective posted
  sales credit memo for your audit trail."
  Source: https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice
- **Production orders:** **Reverse Production Order Transaction** creates item
  journal lines with the opposite sign in quantity, applies them to the original
  entry, and posts the journal (compensating).
  Source: https://learn.microsoft.com/en-us/dynamics365/business-central/production-how-to-reverse-output-posting
- **Correction (negative posting):** the **Correction** field on journal lines posts
  a negative debit instead of a credit (same-line correction), available in all journals.
  Source: https://learn.microsoft.com/id-id/dynamics365/business-central/finance-how-reverse-journal-posting

### Q2. Reversal date rules

- A reverse entry **must have the same document number and posting date as the
  original entry**.
  Source: https://learn.microsoft.com/pt-pt/dynamics365/business-central/finance-how-reverse-journal-posting
- (The same requirement is implied by the reversing-entry rule: same document no.
  + same posting date; "After you reverse an entry, you must make the correct entry.")
- The posting date is additionally constrained by **Allow Posting From / Allow
  Posting To** windows (see Q6): reversing an entry from a closed period is
  blocked unless the window is opened or overridden per user.

### Q3. Reversal of a reversal

- **Not confirmed** for Business Central reversal entries. The docs explicitly say
  "An entry can only be reversed one time" (reverse-via-journal), which leaves
  open whether a *reversal entry* can itself be reversed — no official source found
  stating either way.

### Q4. Audit trail

- The corrective posted credit memo is retained explicitly as the audit trail of
  the correction ("leaves the corrective posted sales credit memo for your audit
  trail"); the original posted invoice keeps its **Canceled** flag.
  Source: https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice
- From the **G/L Register** page you can list entries and use the **Reverse Register**
  action — reversal acts on whole posted journals as a traceable unit.
  Source: https://learn.microsoft.com/en-us/dynamics365-release-plan/2022wave2/smb/dynamics365-business-central/reverse-payment-reconciliation-journal-entries

### Q5. Correction vs reversal vs void vs refund

| Operation | Document state where allowed | Mechanism |
|---|---|---|
| Edit a draft | Draft only | Edit in place |
| Corrective Credit Memo | Posted invoice | New draft memo → edit → post → applied to original (compensating) |
| Cancel posted invoice | Posted invoice | Auto-create+post corrective credit memo + set **Canceled**=Yes on original |
| Reverse Transaction | Posted G/L entries (from a general journal line) | Compensating entry, opposite sign, same doc no./posting date |
| Undo receipt/shipment | Posted but not yet invoiced | **Undo** function (receipt/shipment quantities only) |
| Payment reconciliation reversal | Posted payment reconciliation | **Reverse Register** on the G/L Register page |

Sources: https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice,
https://learn.microsoft.com/en-us/training/modules/correct-entries/4-reverse-journal-postings,
https://learn.microsoft.com/pt-pt/dynamics365/business-central/finance-how-reverse-journal-posting,
https://learn.microsoft.com/en-us/dynamics365/business-central/production-how-to-reverse-output-posting

### Q6. Closed-period P&L protection

- Posting periods are limited on **three levels**: company (General Ledger Setup),
  user (User Setup), journal template. **Allow Posting From / Allow Posting To**
  define the permitted date interval; posting outside it is rejected. A per-user
  override can be granted (e.g., finance staff posting adjustment journals into a
  closed period), then the fields are blanked/restored to company values.
  Sources: https://learn.microsoft.com/is-is/dynamics365/business-central/localfunctionality/belgium/how-to-limit-the-posting-period,
  https://www.azurecurve.co.uk/2024/07/in-microsoft-dynamics-365-business-central-financial-how-do-i-allow-a-user-to-post-into-a-closed-period/
- There is additionally VAT/GST return-period control (**Control VAT Period**:
  block/warn on closed VAT Return Period).
  Source: https://github.com/qpc-github/dynamics365smb-docs/blob/HEAD/business-central/finance-setup-vat.md

### Q7. Concurrency / double-reversal prevention

- "You can reverse an entry only once." / "An entry can only be reversed one time."
- Cancel: "If the **Canceled** field is set to **Yes**, the posted sales invoice is
  already corrected or canceled" — attempting again is blocked/noted.
  Sources: https://learn.microsoft.com/en-us/training/modules/correct-entries/4-reverse-journal-postings,
  https://learn.microsoft.com/en-ca/dynamics365/business-central/sales-how-correct-cancel-sales-invoice

**Notes for erp-kaspi Q1–Q7:** BC is the closest industry reference point for a
"correct vs cancel vs reverse" taxonomy (draft edit → corrective memo → auto-cancel
→ journal reverse). BC's hard rule "same document number + same posting date" for
reversing entries is a data point for the reversal-date question; its "reverse only
once" rule is the double-reversal guard pattern; per-user posting-window override
is a design option for the closed-period problem.

---

## 2. Microsoft Dynamics 365 — Finance (general ledger)

### Q1. Reversal model — compensating journal, linked to the original

- **Reverse journal posting / Reverse entire journal:** creates a **reversal journal
  that mirrors the original journal**; the reversal "is linked to the original
  journal header to ensure full traceability." Individual journal lines can also be
  reversed.
  Sources: https://learn.microsoft.com/en-us/dynamics365/finance/general-ledger/reverse-journal-posting,
  https://learn.microsoft.com/en-us/dynamics365/finance/localizations/turkiye/emea-turkiye-checks-and-promissory-notes
- Checks/promissory notes: "the system automatically creates a reversal journal that
  mirrors the original journal."

### Q2. Reversal date rules

- The dialog offers **Yes** = keep the existing transaction dates, **No** = enter a
  new transaction date. "In some cases, the period of the original transaction might
  be closed, and you must enter a new transaction date for the reversal."
- Reversal **requires an open fiscal period**: "If the reversing date is in a fiscal
  period that isn't open, the transaction can't be reversed"; if the transaction
  supports a reversing date, you can re-date the reversal into an open period.
  Source: https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions

### Q3. Reversal of a reversal — explicitly allowed

- "After the reversal is completed, **a reversed transaction can be reversed again
  (that is, the reversal can be reversed).**" This is explicit for Finance
  (documented in the same "can't reverse" troubleshooting article). Revoking a
  reversed lease transaction works the same way: a journal entry negates the
  reversal's balance impact ("the impact that the reversal had on the balances is
  negated").
  Sources: https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions,
  https://learn.microsoft.com/en-us/dynamics365/finance/asset-leasing/reverse-posted-lease-trans

### Q4. Audit trail

- Reversal journal linked to the original journal header; accessible via the
  **Reversal journal ID** field on the check/promissory journal list page.
- **Reverse tracing** dialog shows both the original and the reversed transactions
  with a linked trace number.
  Source: https://learn.microsoft.com/en-us/dynamics365/finance/asset-leasing/reverse-posted-lease-trans

### Q5. Correction vs reversal vs void vs refund

- Reversal is per-journal/voucher; some transaction types **cannot be reversed at
  all**: bill of exchange, cash register transactions, interest notes, collections
  letters, export shipment, and **revenue-recognition journals** (the schedule
  won't reopen). Electronic vendor payments also can't be reversed.
- Ledger-settled lines can't be reversed until the settlement is reversed; an
  intercompany transaction can't be reversed.
  Source: https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions

### Q6. Closed-period P&L protection

- Fiscal periods can be **on hold** or **permanently closed**; reversal into a
  closed period is blocked (must re-date to an open period).
- Additionally, if the **year-end close process** has run for the fiscal year, the
  transaction can't be reversed even if some period is still open — year-end close
  is a separate, stronger lock that itself must be reversed first.
  Source: https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions

### Q7. Concurrency / double-reversal prevention

- "Transaction reversal is already in process: If the transaction is in the process
  of being reversed, that process must be completed. **A separate reversal process
  can't be done.**" — an explicit in-flight guard.
  Source: https://learn.microsoft.com/vi-vn/troubleshoot/dynamics-365/finance/general-ledger/cant-reverse-transactions

**Notes for erp-kaspi Q1–Q7:** Finance documents the exact policy erp-kaspi lacks —
reversal-of-reversal **is allowed** (a chain of compensating entries), reversal date
**defaults to the original date but must move to an open period when the original
is closed**, and an in-flight guard prevents concurrent double reversals. The
year-end-close lock is a two-tier period protection worth mirroring.

---

## 3. Odoo (Accounting, 15/17/18)

### Q1. Reversal model — compensating journal entry + Reset to Draft for correctable states

- **Reverse Entry** wizard (`account.move.reversal`): creates a new journal entry
  that mirrors the original with **debit and credit amounts swapped**; the
  Reference indicates "Reversal of: <original>"; the Other Info tab keeps a link to
  the original entry (and the new move sets `reversed_entry_id`).
  Sources: https://hibou.io/docs/accounting-3/journal-entries-90/ver/14-0-21,
  https://www.odoo.com/forum/help-1/can-i-reset-an-invoice-i-already-submitted-via-zatca-to-correct-the-address-322079
- **Storno accounting** (optional setting, required in several EU/Eastern-European
  localizations): the copied entry keeps amounts but **negative** — "negative credit
  or debit amounts … counterbalance initial journal entries."
  Source: https://www.cybrosys.com/blog/how-to-manage-reversal-of-the-journal-entries-in-odoo-17-storno-accounting
- **Reset to Draft** (`button_draft`): a posted move can be reset to draft and
  edited/re-posted, **unless** the journal is configured "Lock Posted Entries with
  Hash" (inalterable hash), the move was submitted to a tax authority / e-invoicing,
  or a localization hook blocks it. Resetting to draft **unreconciles linked
  payments automatically**.
  Sources: https://www.odoo.com/forum/help-1/journal-change-after-post-205086,
  https://www.odoo.com/forum/help-1/what-determines-when-i-can-see-reset-to-draft-on-a-customer-invoice-313171

### Q2. Reversal date rules

- The wizard offers **Specific** (default: current date) or **Journal Entry Date**
  (the original entry's date). "Specific" allows posting into "any open accounting
  period."
- **Lock dates prevent reversals from hitting closed periods:** a lock date
  "prevents modifications to any posted journal entries with an accounting date on
  or before the lock date" and "prevents posting new entries with an accounting
  date on or before the lock date. In such cases, the system automatically sets the
  accounting date to the day after the lock date."
  Sources: https://hibou.io/docs/accounting-3/canceling-journal-entries-80,
  https://www.odoo.com/documentation/18.0/applications/finance/accounting/reporting/year_end.html

### Q3. Reversal of a reversal

- **Not confirmed.** No official Odoo documentation found explicitly allowing or
  forbidding reversal of a reversal via the standard wizard. The community guide
  mentions a "Reversals" stat button linking moves, but the source is a third-party
  Odoo customisation write-up (jito-dev GUIDANCE.md), not standard Odoo — treat as
  unverified for stock Odoo.

### Q4. Audit trail

- The reversal move carries `reversed_entry_id` (field referenced in community
  automation playbooks) and the reference "Reversal of: INV/xxxx, <reason>" — the
  reason from the wizard lands in the reversal's `ref`.
- Credit notes created by reversal are numbered **R + invoice number**.
- "Reversed." label shows on the original invoice's payment status line.
  Sources: https://www.odoo.com/forum/help-1/can-i-reset-an-invoice-i-already-submitted-via-zatca-to-correct-the-address-322079,
  https://www.slideshare.net/slideshow/how-to-manage-credit-notes-refunds-in-odoo-18-accounting/285990172
  (credit-note numbering R+invoice no. and "Reversed." label per community slideshare;
  treat as widely documented behavior, not verbatim official docs).

### Q5. Correction vs reversal vs void vs refund

- **Draft:** edit freely.
- **Reset to Draft:** correct a *posted* invoice/journal by resetting to draft
  (conditions as above; reconciliation is undone automatically).
- **Reverse (credit note):** "This is the only option for invoices marked as
  *in payment* or *paid*." Two modes: **Reverse** (draft credit note, editable —
  used for partial credit) and **Reverse and Create Invoice** (validated credit
  note auto-reconciled against the original + a new draft invoice prefilled from
  the original).
- **Full Refund:** creates a credit note, auto-validates, reconciles with the
  invoice — "the option to choose for a full refund or to **cancel** a *validated*
  invoice."
- **Debit Note** for increases; vendor refunds recorded the same way (credit notes).
- Issuing a credit/debit note creates a **reverse entry that zeroes out the journal
  items** generated by the original invoice.
  Source: https://documentation.erptoancau.com/17.0/applications/finance/accounting/customer_invoices/credit_notes.html
  (mirror of official Odoo 17 docs; official URL pattern is
  odoo.com/documentation/17.0/applications/finance/accounting/customer_invoices/credit_notes.html — not opened directly in this research, so mirror citation is used)

### Q6. Closed-period P&L protection

- **Lock Everything date** / Journal Entries Lock Date: prevents creating or
  modifying entries on/before the lock date; new entries get their accounting date
  auto-pushed to the day after the lock date. Users with *administrator* rights can
  still create entries only if an exception is configured.
  Source: https://www.odoo.com/documentation/18.0/applications/finance/accounting/reporting/year_end.html
- Users from the *accountant* access group may still create/modify under the
  Journal Entries Lock Date (per saas-17.4 year-end docs).
  Source: https://www.odoo.com/documentation/saas-17.4/nl/applications/finance/accounting/reporting/year_end.html

### Q7. Concurrency / double-reversal prevention

- **Not confirmed.** No official source found describing an explicit guard against
  running the reversal wizard twice on the same move. (Stock behavior per community
  write-ups: the original gets a "Reversals" stat button, but no documented
  idempotency check was found in official docs.)
- Hash-locked ("Lock Posted Entries with Hash") journals prevent Reset-to-Draft
  tampering, which is the draft-correction-side guard.
  Source: https://www.odoo.com/forum/help-1/journal-change-after-post-205086

**Notes for erp-kaspi Q1–Q7:** Odoo is the strongest reference for the
*compensating-entry* model: posted moves are immutable; correction of a posted move
happens either via a controlled Reset to Draft (with explicit block conditions) or
via a reversal move with a `reversed_entry_id` backlink. The auto-date-shift past
the lock date is an alternative to rejecting backdated reversals outright. Open
question Q3 (reversal-of-reversal) is genuinely unanswered by Odoo docs.

---

## 4. NetSuite (Journal reversal / voiding)

### Q1. Reversal model — configurable: zero-out void OR reversing-journal void (status-flip + compensating)

- Default **Void**: "sets its total and all its line items to 0 but doesn't remove
  it from the system. This is the preferred way to cancel an existing transaction
  because the audit trail is preserved." After voiding, no GL-impacting changes can
  be made to the original (including changing the posting period).
- With **Void Transactions Using Reversing Journals** preference (Setup >
  Accounting > Accounting Preferences): clicking Void creates a **reversing
  journal entry** — "a journal entry with the opposite amount to cancel the general
  ledger impact"; the **Void Of** field links to the original; saving sets the
  original's status to **Voided**, and the **Voided On** date links back to the
  voiding journal.
- Journal-entry forms have their own Void button (shown only when the reversing-
  journal preference is *disabled*).
  Sources: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N563543.html,
  https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N1469460.html
- **GL Impact Locking** (separate feature): any change to a GL-impacting
  transaction automatically generates a GL impact adjustment **copy** and a GL
  impact adjustment **reversal** transaction, linked with a trace number.
  Source: https://nlcorp.app.netsuite.com/core/media/media.nl?_xt=.pdf&c=NLCORP&h=4611c8c7148a3cf75ad1&id=90178811

### Q2. Reversal date rules

- The whole point of the reversing-journal preference is to "permit the creation of
  journal entries that void transactions on **days or periods different from the
  original transaction dates**."
- Preference **Set Reversal Variance Date Equal to the Reversing Journal Date When
  Voided Transaction Is in a Closed Period**: uses the reversing journal's date
  (rather than the current date) for variance/reversal lines.
- **Closed periods can't receive voids:** "To void a check from a closed period,
  you need to reopen the period first."
  Sources: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N1469460.html,
  https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3951602420.html,
  https://nlcorp.app.netsuite.com/core/media/media.nl?_xt=.pdf&c=NLCORP&h=4611c8c7148a3cf75ad1&id=90178811

### Q3. Reversal of a reversal

- **Not confirmed.** No NetSuite help source found explicitly covering reversal of a
  voiding/reversing journal.

### Q4. Audit trail

- Original ↔ voiding journal are cross-linked (**Void Of** / **Voided On** fields).
- **System Notes** subtab on every transaction: field-level change history (who,
  when, old/new value, context — UI/web services/SuiteScript).
- **Transaction Audit Trail**: Transactions > Management > View Audit Trail.
- Deleted records are retrievable via the Deleted Record search type; the vendor
  prepayment docs state explicitly "The NetSuite Audit Trail registers the delete
  operation."
  Sources: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N1469460.html,
  https://www.getgsi.com/blog/en-us/netsuiteblog/netsuite-auditing-and-data-management-features,
  https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156380292896.html

### Q5. Correction vs reversal vs void vs refund

- **Void** (preferred cancel): zero-out, record preserved, GL-impact frozen.
- **Delete:** removes the transaction (restricted: can't delete from a closed
  period; only admins can delete from a locked period; audit trail registers the
  delete).
- **Close:** separate operation (closing transactions/lines).
- **Refund:** dedicated record types (Customer Refund); "Transaction Already
  refunded" is a system error — refunds are tracked as one-per-transaction.
  Sources: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N563543.html,
  https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156380292896.html,
  https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N3539978.html

### Q6. Closed-period P&L protection

- **Manage Accounting Periods**: closed periods reject GL-impact changes with hard
  errors, e.g. TRAN_PERIOD_CLOSED: "You cannot change the G/L impact of a
  transaction in a closed period"; TRANS_EDIT_DISALLWD: "This transaction is in a
  period that has been closed. You may not edit it."
  Source: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N3539978.html
- Period-close checklist tasks (A/R locked etc.) can block posting per module.

### Q7. Concurrency / double-reversal prevention

- Explicit system error **TRANS_ALREADY_VOIDED**: "Transaction Already Voided. Void
  failed because transaction is already voided." Similarly
  **TRANS_ALREADY_REFUNDED** and **TRANS_ALREADY_SETTLED** ("Void failed because
  transaction has already settled, submit credit.").
  Source: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N3539978.html

**Notes for erp-kaspi Q1–Q7:** NetSuite demonstrates a *configurable* model
(zero-out void vs reversing-journal void) with the period question handled by
explicit preference. Its explicit double-void/refund error codes are the pattern
for erp-kaspi's idempotency requirement; the "voided transaction becomes GL-frozen"
rule is the closest analog to protecting posted history.

---

## 5. QuickBooks Online

### Q1. Reversal model — hybrid: void (zero-out status-flip), delete, reverse (compensating journal)

- **Void:** "changes the transaction amount to zero and retains the original
  transaction date. The transaction is not removed, so you have it for your
  records." Not all transaction types can be voided (e.g., can't void a deposit, a
  payroll payment, or a VAT payment).
- **Delete:** "removes the transaction from QuickBooks" (audit log retains a trace).
  "After you delete a transaction, you can't get it back."
- **Reverse (journal entries):** "creates a new entry that offsets the original
  transaction without deleting it" — the reversed entry often carries the original
  reference number with an "R" (e.g. *Journal 123 < Journal 123R*).
  Sources: https://plugin-qbo.intuit.com/qbo-ush/helpcontent/production/124/en_GB/QBO_Plus/Content/Topics/account/help_account_registers_delete.htm,
  https://quickbooks.intuit.com/learn-support/en-ca/help-article/list-management/void-delete-transactions-quickbooks-online/L5sZV8GYh_CA_en_CA,
  https://www.dancingnumbers.com/edit-delete-a-journal-entry-in-quickbooks/

### Q2. Reversal date rules

- Void **retains the original transaction date** (no re-dating of the void itself);
  reversals via a reversing journal are new entries dated like any other journal.
- The **closing date** governs what can be touched: "You can't edit any transactions
  before the closing date" (warning or password required).

### Q3. Reversal of a reversal

- **Not confirmed.** No Intuit source found on reversing a reversal (e.g., via a
  second "Reverse" on an R-suffixed journal).

### Q4. Audit trail

- Voided transactions "remain visible in reports with $0.00 value"; deleted
  transactions are "only visible in Audit Log."
  Source: https://www.dancingnumbers.com/delete-transactions-in-quickbooks/
- The Audit Log records who changed what and when (Intuit's official close-books
  article emphasizes the immutable audit trail as the reason to lock).
  Source: https://quickbooks.intuit.com/learn-support/en-sg/help-article/close-books/close-books-quickbooks-online/L59LelyPM_SG_en_SG

### Q5. Correction vs reversal vs void vs refund

- Edit freely only before the closing date (warning/password gate applies).
- Void: error or premature entry, keep the record (checks, invoices, bill payments,
  payments, sales orders, expenses can be voided; **only void invoices, payments,
  sales orders** among sales transactions).
- Delete: duplicate/typo/test entries; irreversible.
- Credit memo / refund receipt handle customer refunds (separate record types).
- Voiding an invoice does **not** auto-void its payments (handle separately);
  voided invoice returns linked items to stock; payments become unapplied.
  Sources: https://quickbooks.intuit.com/learn-support/en-ca/help-article/list-management/void-delete-transactions-quickbooks-online/L5sZV8GYh_CA_en_CA,
  https://quickbooks.intuit.com/learn-support/en-au/manage-customers-and-income/re-understanding-the-difference-between-voiding-vs-deleting/01/1495907

### Q6. Closed-period P&L protection

- **Close the books** (Advanced > Accounting): set a **Closing date**; options are
  "Allow changes after viewing a warning" or "…and entering password." Any attempt
  to change transactions dated on or before the lock date triggers the warning or
  password prompt. Only primary/company admins can change the setting.
  Source: https://quickbooks.intuit.com/learn-support/en-sg/help-article/close-books/close-books-quickbooks-online/L59LelyPM_SG_en_SG

### Q7. Concurrency / double-reversal prevention

- **Not confirmed.** No Intuit-documented idempotency check found for double voids
  or double journal reversals (the UI is single-user flow; the API has
  idempotency-key support per third-party Xero/QB skill notes, but that's out of
  the QBO official-doc scope used here).

**Notes for erp-kaspi Q1–Q7:** QBO is the clearest example of *void = zero-out
status-flip retaining the original date* vs *reverse = separate compensating
journal* — a useful vocabulary split for the decision memo. Its password-gated
closing date is the SME-friendly version of period locking.

---

## 6. Xero

### Q1. Reversal model — status-flip (void), no compensating journal entries

- **Draft / awaiting-approval invoices: Delete.** Approved (AUTHORISED) invoices
  **cannot be deleted**; they can only be set to **VOIDED**. VOIDED invoices stay
  visible in lists; invoice numbering continues unbroken.
- Paid invoices: remove the payment first (Remove & Redo), then void; or use a
  **credit note** (separate AR/AP document) allocated against the invoice — the
  credit-note flow is the compensating mechanism.
- API mirrors this: DRAFT/SUBMITTED → `DELETED`; AUTHORISED → `VOIDED`;
  AUTHORISED can never return to DRAFT; PAID is system-set and cannot be written.
  Sources: https://central.xero.com/0/article/Delete-or-void-a-sales-invoice,
  https://github.com/epiusecx/cohenix_bma_xero/blob/HEAD/xero/docs/official_xero_api_docs/Invoices.md
  (mirrors Xero API docs; official API docs at developer.xero.com — not opened here,
  behavior confirmed by Xero Central help article above)

### Q2. Reversal date rules

- Void keeps the record's original date — it is a status change, not a dated
  transaction.
- A **lock date blocks voids/edits of records dated before it**: "A bill dated in
  a locked period cannot be voided, full stop." Standard remedy: issue a **credit
  note dated after the lock date** to reverse the effect.
  Source: https://github.com/aspiers/ai-config/blob/HEAD/.agents/skills/xero-browser/SKILL.md
  (community skill quoting observed Xero error text; Xero Central's own lock-date
  guidance confirms locks stop users adding/changing transactions on or before the
  lock date — official Xero Central lock-date article)
- API also errors when "The document date cannot be before the period lock date."
  Source: https://support.splose.com/en/articles/4735175-why-an-invoice-fails-to-sync-with-xero-and-how-to-fix-it

### Q3. Reversal of a reversal

- **Not confirmed.** Voided invoices cannot be un-voided (they can only be
  *copied* into a new invoice); a voided credit note is a terminal state as far as
  documented flows go. No documented "un-void" operation.

### Q4. Audit trail

- Every transaction has a **History & Notes** section logging each action with
  user and timestamp (including who voided and when).
- Deleted invoices are retrievable via search ("Include Deleted & Voided") and can
  be copied; no in-app recovery of deleted drafts.
  Sources: https://central.xero.com/0/article/Delete-or-void-a-sales-invoice,
  https://medium.com/@wowzertechnologies/how-to-recover-deleted-xero-invoices-without-losing-audit-history-wow-backup-restore-f7ed9e22cccf

### Q5. Correction vs reversal vs void vs refund

- **Draft/awaiting approval:** Delete (in bulk supported).
- **Authorised, awaiting payment:** Void (record retained, status change).
- **Paid/partly paid:** remove the payment (Remove & Redo) first, then void; or
  leave paid and raise a **credit note** (documented scenarios: invoicing mistake
  → credit note applied reducing amount owed; goods returned after payment → credit
  held for future invoices; discount → credit note against a discounts account).
- **Credit notes** are their own document type with allocate/void lifecycle, not a
  status of the invoice.
  Sources: https://central.xero.com/0/article/Delete-or-void-a-sales-invoice,
  https://xero.my.site.com/s/article/Common-scenarios-for-credit-notes-US,
  https://central.xero.com/0/article/Remove-payment-from-an-invoice-or-bill

### Q6. Closed-period P&L protection

- **Lock Dates** (Accounting > Advanced > Financial settings): stop users adding
  or changing transactions dated on or before the lock date. Two levels: adviser
  lock (all users) and general lock (non-adviser users).
  Source: https://www.caman.au/wp-content/uploads/2023/06/Xero-Accounts-EOY-2023.pdf
  (accountant EOY guide quoting Xero's lock-date behavior; consistent with Xero Central
  Financial-settings help)

### Q7. Concurrency / double-reversal prevention

- Partial: validation errors exist for illegal state transitions — "The status
  VOIDED cannot be applied to the invoice because it has payments or credit notes
  allocated to it"; a credit note permanently locks its invoice (can't be edited or
  deleted even after the credit note is voided).
- No documented double-void idempotency primitive beyond these state validations.
  Source: https://support.splose.com/en/articles/4735175-why-an-invoice-fails-to-sync-with-xero-and-how-to-fix-it

**Notes for erp-kaspi Q1–Q7:** Xero is the purest *status-flip* system: no
compensating journal entries at all; reversal effects are achieved via credit
notes (separate documents) and voids (status change). It shows that a status-based
model works if the state machine is strict (AUTHORISED→VOIDED only, no delete of
authorised docs) and lock dates are hard gates.

---

## 7. Cross-system comparison for the erp-kaspi decision memo

| System | Reversal model | Reversal date | Reversal-of-reversal | Double-reversal guard | Period protection |
|---|---|---|---|---|---|
| BC | Hybrid: compensating entry (journals) + corrective credit memo + Canceled flag | Same posting date & doc no. as original | Not confirmed | "Reverse only once"; Canceled-flag check | Allow Posting From/To (company/user/template), VAT periods |
| D365 Finance | Compensating reversal journal, linked to original header | Original date preferred; must move to open period if closed | **Allowed explicitly** | In-flight "reversal in process" guard | On-hold/permanently-closed periods; year-end close as second lock tier |
| Odoo | Compensating entry (swap or Storno negatives); Reset to Draft for correctable states | Specific (current) or original date; auto-shifted past lock date | Not confirmed | **Not confirmed** | Lock Everything / Journal Entries Lock Date |
| NetSuite | Configurable: zero-out void OR reversing journal; GL-impact frozen | Voiding journal date (may differ from original) | Not confirmed | TRANS_ALREADY_VOIDED / TRANS_ALREADY_REFUNDED errors | Closed accounting periods; locked periods (admin-only delete) |
| QuickBooks | Void = zero-out status-flip (keeps original date); Delete; Reverse = compensating journal (R-ref) | Void keeps original date; reversals dated as new journals | Not confirmed | **Not confirmed** | Closing date + optional password |
| Xero | Status-flip void (no compensating journals); credit notes as separate compensating docs | Void has no date; lock date blocks voids before it | Not confirmed (void is terminal) | State-transition validations only | Lock dates (adviser vs all-user) |

### Patterns relevant to erp-kaspi owner questions

1. **Q1 (reversal model):** No major system does pure status-flip *of amounts* on a
   posted GL entry. Status-flip is used at the **document** level (void/canceled
   flags) while the **ledger** impact is always neutralized by a compensating entry
   (D365 Finance/BC/Odoo) or a zero-out that keeps the record (NetSuite/QBO), or a
   separate credit-note document (Xero). A design that flips a status and thereby
   deletes the ledger effect has no precedent here.
2. **Q2 (reversal date):** Two schools: BC requires the **same posting date** as
   the original (historical correction); D365 Finance **prefers the original date
   but forces an open-period date** when the original period is closed; Odoo
   defaults to **current date** and auto-shifts past lock dates; NetSuite makes it
   configurable. All agree: **reversals must not land in a closed/locked period.**
3. **Q3 (reversal-of-reversal):** Only D365 Finance explicitly allows it
   ("the reversal can be reversed"); others are silent. If erp-kaspi allows it,
   Finance's chain-of-compensating-entries with trace links is the model.
4. **Q4 (audit trail):** Universal pattern is **bidirectional links**:
   reversal↔original (Void Of/Voided On, reversed_entry_id, Reversal journal ID,
   corrective credit memo references). Plus a per-record change log (Xero History
   & Notes, NetSuite System Notes).
5. **Q5 (correction vs reversal vs void vs refund):** The shared taxonomy is:
   draft → edit in place; posted-but-fixable → reset/corrective document;
   posted-and-wrong → reversal (compensating); cancel-with-record → void/cancel
   flag; money back to customer → refund/credit note (separate document type).
6. **Q6 (P&L preservation):** Every system protects historical P&L with a
   **date-based posting lock** (lock dates, closing date, Allow Posting windows,
   closed accounting periods, year-end close). None allows edits into a locked
   period without an explicit override.
7. **Q7 (idempotency/concurrency):** Documented guards are state-based:
   "already voided" errors (NetSuite), "reverse only once" (BC),
   in-flight-reversal guard (D365 Finance), hash locks on posted entries (Odoo).
   No system documents an optimistic-concurrency token for reversals in the
   official user docs surveyed; erp-kaspi's idempotency-key approach goes beyond
   what's publicly documented for these products.
