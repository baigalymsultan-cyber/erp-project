# SAP document reversal — research for erp-kaspi reversal decision memo

**Date:** 2026-10-03
**Method:** web research (browser.search), official SAP sources preferred; community.sap.com and
third-party how-tos used where official docs were not directly retrievable. Claims marked
[OFFICIAL], [COMMUNITY], [THIRD-PARTY], or [UNCERTAIN].
**Constraint:** this is RESEARCH ONLY. No code changes were made.

> Core SAP principle: **a posted document is never modified or deleted.**
> A reversal posts a *new* document that offsets the original. The original keeps its amounts,
> posting date and period forever. This is SAP's answer to "compensating journal vs status flip":
> **compensating journal, with the original stamped as reversed.**

---

## 1. How SAP reverses a posted document

### Observed behavior

- **The original document is never changed or deleted.** FB08 (individual reversal) / F.80 (mass
  reversal) / FBRA (reset+reverse cleared docs) post a **new reversal document** whose debit/credit
  postings are the opposite of the original's. The original stays in the ledger exactly as posted.
  [THIRD-PARTY][COMMUNITY]
- The reversal creates a new FI document number (status bar: "Document XXXXXXXXX was posted in
  company code XXXX"); in S/4HANA Fiori the equivalent is **"Reverse Journal Entries" / "Reverse
  Line Items"** apps: select the journal entry → *Reverse* → enter reversal reason + posting date →
  post. [THIRD-PARTY][COMMUNITY]
- **Prerequisites enforced by SAP before a document can be reversed** (from an SCEIS/SAP FI
  operating guide): the document must (a) contain **no cleared items**, (b) contain only
  vendor/customer/G-L line items, (c) be **"not a reversing document or already reversed"**,
  (d) be of a document type that allows reversal, (e) have been posted within FI (error F5673
  "Not possible to reverse the document in financial accounting" if the document came from another
  module and was not posted in FI — "Reverse the original transaction" instead). [COMMUNITY/OPERATING-DOC]
- **Original gets marked as reversed:** on the header of the original document (BKPF) the fields
  **STBLG** (reversal document number) and **STJAH/STGJA** (fiscal year of the reversal document)
  are filled; FB03 shows the link ("Reversed by" / header data). Line-item level indicator
  **BSEG-XRAGL** also marks reversed items; BKPF-XREVERSAL = '1' means "document has been reversed",
  = '2' means "this document IS the reversal document". [COMMUNITY]
- **Linking is bidirectional:** the reversal document's STBLG/STJAH point back at the original,
  and the original's STBLG points at the reversal. In FB03 the user can jump between them via the
  "reversal document" button. Note caveat: for documents originating in MM/SD, STBLG may be filled
  in the *source* module's tables (RBKP for MM invoices) rather than on the FI follow-on documents
  — the relation lives at the source document level. [COMMUNITY]
- **Negative postings (Storno):** depending on the reversal-reason configuration, SAP posts either
  a **normal reversal** (opposite side: original debit → reversal credit, which inflates turnover on
  the account) or a **negative posting** ("Storno"): the reversal keeps the *same* debit/credit
  indicator as the original line but carries a **negative amount** (BSEG-SHKZG = 'S'/'H' with
  negative sign), so the account's debit/credit totals are reduced instead of increased and net is
  zero. The Fiori guide phrases it: the reversal-reason selection lets the system "give the
  transaction figures (following the reversal) the status they would have had without posting the
  reversed document and its reversal document" — that is negative posting. [THIRD-PARTY][COMMUNITY]

### Sources
- https://www.guru99.com/how-to-perform-document-reversal.html (FB08 steps, reversal reasons,
  posted document cannot be deleted, original marked reversed)
- https://www.stechies.com/amp/document-reversal-sap/ (FB08 procedure, new reversal document number)
- https://community.sap.com/t5/application-development-discussions/payment-document-reversed/td-p/6586971
  (accepted solution: Doc1 reversed by Doc2 → XREVERSAL=1/2, linked via STBLG and STJAH)
- https://community.sap.com/t5/application-development-discussions/how-to-find-fi-documentument-is-reversed-or-not/m-p/6863633
  (STBLG/STJAH bidirectional links; XSTOV/XREVERSAL flags)
- https://community.sap.com/t5/application-development-and-automation-discussions/fi-how-to-exclude-accounting-document-that-have-reversed/td-p/3541873
  (STBLG empty = not reversed; BSEG-XRAGL identifies reversed open items)
- https://community.sap.com/t5/enterprise-resource-planning-q-a/mr8m-reversal-and-field-stblg/qaq-p/4033656
  (MM vs FI reversal linking logic)
- https://community.sap.com/t5/enterprise-resource-planning-q-a/reversal-reason/qaq-p/5787058
  (negative posting / alternative posting date are per-reason config flags)
- https://community.sap.com/t5/enterprise-resource-planning-q-a/unable-to-reverse-fi-document-in-t-code-fb08/qaq-p/9218212
  (F5673: documents not posted in FI cannot be reversed in FI — reverse the source)
- https://cg.sc.gov/sites/cg/files/Documents/Guidance%20and%20Forms%20for%20State%20Agencies/CGs%20Accounting%20Policies%20and%20Procedures/Accounting%20Guidance/How%20to%20Record%20Document%20Reversals%20Using%20SCEIS%20Transaction%20Code%20FB08.pdf
  (FB08 prerequisites: no cleared items, not already reversed/reversing, reversible doc type;
  reversal in same period or override posting date)
- https://www.scribd.com/document/521611001/IESCO-SAP-ERP-FI-TD-Fiori-GL1
  ("Reverse Posted GL voucher": posting reversal adds info to existing documents; negative posting
  = transaction figures keep the status as if original+reversal never existed)

### Informs owner questions
- **Reversal model:** SAP evidence strongly favors *compensating-journal reversal with the original
  stamped as reversed* over a bare status flip. The status flip on the original (STBLG/XREVERSAL)
  exists, but it is a *marker/annotation* — the financial effect comes from the new document.
  A status-flip-only design would be unrecognizable to SAP auditors: there would be no second
  document to display in FB03.
- **Audit trail:** suggests the design wants (a) a `reversed_by` FK on the original operation,
  (b) a `reverses` FK on the reversal operation, (c) a `reversal_indicator` on both sides
  ('1' reversed / '2' reversal), (d) amounts never mutated.

---

## 2. Reversal date rules

### Observed behavior

- **Default: the reversal takes the original document's posting date.** FB08's Posting Date field is
  optional — left blank, it accepts "the original document's posting date". The period of the
  original document must be **open** in OB52 for this to work. [THIRD-PARTY][COMMUNITY]
- **Reversal reasons control date behavior.** Standard reasons 01–04:
  - 01 *Reversal in current period*, 02 *Reversal in closed period*,
  - 03 *Actual reversal in current period*, 04 *Actual reversal in closed period*.
  Behind them there are only two config flags per reason (SPRO → Financial Accounting → G/L
  Accounting → Business Transactions → Adjustment Posting/Reversal → **Define Reasons for Reversal**):
  **"Negative postings" allowed** and **"Alternative posting date" allowed**. Reasons 01 vs 03 (and
  02 vs 04) behave identically in mechanics — the difference is only naming/audit classification.
  [COMMUNITY]
- **Closed period → must use an alternative date.** If the original period is closed, the reversal
  must be posted into an open period (normally the current period) with a reason that allows an
  alternative posting date (e.g., 02/04). Attempting to post into a closed period fails with a
  posting-date error (e.g., EP443: "Posting date for reversal document is before [the latest
  posting date / closed period]"). **You cannot reverse into a closed period.** [COMMUNITY][THIRD-PARTY]
- **Posting-period control (OB52)** is what protects periods: OB52 "Posting Periods: Specify Time
  Intervals" opens/closes periods per posting-period variant and account type; it is the hard gate —
  posting (including reversal) into a closed period is rejected, not silently adjusted.
  [COMMUNITY]
- Note on negative postings across years: negative postings are only meaningful/allowed within the
  current or following fiscal year context [UNCERTAIN — frequently asserted in practitioner docs,
  not confirmed against an official source in this research].

### Sources
- https://www.guru99.com/how-to-perform-document-reversal.html (posting date optional; reason 02/04
  for closed periods → post in current open period)
- https://www.slideshare.net/Amith_Sanghvi/fb08-how-to-reverse-a-document-in-sap-t-code
  ("Leave blank to accept the original document's posting date or enter the desired posting date")
- https://community.sap.com/t5/enterprise-resource-planning-q-a/reversal-reason/qaq-p/5787058
  (only two functional flags: negative posting allowed / alternative posting date allowed;
  01&03 / 02&04 functionally identical)
- https://www.stechies.com/amp/steps-closing-period-mm-fi-co/ (OB52 overview)
- https://community.sap.com:443/t5/enterprise-resource-planning-q-a/how-to-reverse-gr-for-last-year/qaq-p/12498820/comment-id/4066925
  (fiscal-year-closed GR: post the reversal with the current date in the current period —
  no way to post into the closed year)
- https://www.caclubindia.com/experts/sap-2035724.asp
  ("Once the period is closed you cannot pass the entries in 31/3/2014 … use FB08 with suitable
  posting and reversal reason" — i.e., post into the open period)
- https://www.erplingo.com/sap-error-messages/en/ep/ep443 (EP443: reversal posting date earlier
  than allowed → closed periods block the reversal date)

### Informs owner questions
- **Reversal date:** SAP default = original posting date *if the period is open*, else *current
  open-period date*. Design implication for erp-kaspi: reversal-date rule should be
  `min(original_date, current_open_period)`-style — never allow backdating into a closed period.
  This also interacts with "historical P&L preservation": SAP achieves P&L immutability of closed
  periods by *blocking* postings there entirely (OB52), not by special reversal logic.

---

## 3. Reversal of a reversal

### Observed behavior

- **SAP refuses to reverse a document that is itself a reversal** via FB08/FBRA. A reversal
  document is not a reversible document type (document-type configuration OBA7 designates which
  types are reversible), and the FB08 precondition check explicitly rejects "a reversing document
  or already reversed" documents. Community evidence: attempting it in FBRA produces
  "the reversal document cannot be reset". [COMMUNITY]
- **The documented workaround is a manual offsetting posting:** practitioners reverse a mistaken
  reversal by posting a *new* document with reference to the reversal document using F-02 with the
  "Generate reverse posting" option (or FBR2 / "Post with reference"). This is a *fresh manual
  journal*, not a first-class "reversal of reversal" — it creates a third document that offsets
  the second. [COMMUNITY]
- So SAP's model is a **chain of documents** (original → reversal → manual offset), never an
  in-place undo of the undo.

### Sources
- https://community.sap.com/t5/enterprise-resource-planning-q-a/fbra-reversed-document-reversal-issue/qaq-p/12679926/comment-id/4129544
  (reversing an already-reversed document → error "the reversal document cannot be reset")
- https://community.sap.com/t5/financial-management-q-a/reverse-the-reversal-fm-budget-document/qaa-p/12637377
  (FMDOCREV allows only original budget entries, not already-reversed ones)
- https://www.scribd.com/document/549103969/Sapfi-Class
  ("Reversal of Reversed Document — Indirect method (work around)": F-02 → "post with reference"
  → "Generate reverse posting")
- https://www.slideshare.net/slideshow/sap-fico-general-ledger-account-ii/10663895
  ("Reverse of reversal: T.code F-02 → Post with reference → Generate reverse posting")
- https://cg.sc.gov/.../How%20to%20Record%20Document%20Reversals%20Using%20SCEIS%20Transaction%20Code%20FB08.pdf
  (FB08 precondition: "Is not a reversing document or already reversed")

### Informs owner questions
- **Reversal-of-reversal:** SAP says NO at the system level; undoing an erroneous reversal is done
  by posting a *third, manual offsetting document*. For erp-kaspi this means: do not design
  `reverse()` on a reversal operation; instead offer `create_offsetting_operation()` that references
  the reversal as its source. Also confirms that reversal chains (A→B→C) are expected data, not an
  error state.

---

## 4. Audit trail of a reversal

### Observed behavior

- **Two cross-referencing header records:** BKPF-STBLG/STJAH on the original (points to the
  reversal), and the same fields on the reversal (point back to the original). In FB03 these appear
  as "Reversed by" links with clickable navigation between documents. [COMMUNITY]
- **Reversal indicators:** BKPF-XREVERSAL = '1' (reversed) / '2' (reversal document); line-item
  level BSEG-XRAGL identifies reversed items. Auditors query BKPF where XREVERSAL = 1 or 2 to list
  all reversal activity. [COMMUNITY]
- **The reversal carries its own reason code** (BKPF-STGRD / reversal reason 01–05), recording *why*
  it was reversed — a configurable, reportable attribute. [THIRD-PARTY][COMMUNITY]
- **Document flow:** for source-module documents (SD/MM), SAP keeps a document-flow chain from the
  sales order → delivery → billing → FI document → reversal, so auditors can trace the full lineage
  across modules. [COMMUNITY]
- **Change logs:** header stores USNAM (created by), CPUDT/CPUTM (entry date/time); BKPF also has
  changed-by/on fields. Dedicated change-document logging (CDHDR/CDPOS) covers master-data and
  header changes where enabled. [UNCERTAIN on exact CDHDR coverage for reversal transactions —
  reversal *creates* rather than *changes*, so the audit story is the document chain itself, not a
  field-change log.]

### Sources
- https://community.sap.com/t5/application-development-discussions/payment-document-reversed/td-p/6586971
  (accepted solution: XREVERSAL 1/2 + STBLG/STJAH linking)
- https://community.sap.com/t5/enterprise-resource-planning-q-a/how-to-display-all-reversal-document/qaq-p/2950003
  ("Go to table BKPF and select field XREVERSAL and enter 1 or 2. This will list all reversal
  documents. Indicator as to whether a document has been reversed ('1') or is a reversal document ('2').")
- https://community.sap.com/t5/enterprise-resource-planning-q-a/unable-to-reverse-fi-document-in-t-code-fb08/qaq-p/9218212
  ("The system then automatically links the reversal document with the original document … to
  reverse in FI"; SCEIS guide language: "the system then automatically links the reversal
  document with the original document, this maintains a clear audit trail")
- https://www.guru99.com/how-to-perform-document-reversal.html (verify via FB03; original shows
  as reversed)

### Informs owner questions
- **Audit trail:** an erp-kaspi reversal should produce (1) bidirectional links (original→reversal,
  reversal→original), (2) a reversal indicator on both sides, (3) a reversal *reason* attribute on
  the reversal, (4) preserved who/when on both headers. These are exactly the audit artifacts SAP
  auditors ask for — cheaper than inventing a custom trail format.

---

## 5. Correction vs reversal vs cancellation (void) vs refund

### Observed behavior

- **Change (FB02 / "correcting document"):** a posted document can be *changed* only in fields that
  do not affect amounts, accounts, or balances — typically document header text, reference number,
  assignment, trading-partner fields (and only where field status/configuration allows). Posting
  date, amounts, accounts, cost centers cannot be changed after posting. Anything beyond that
  requires reversal + repost. [COMMUNITY/GENERAL — exact changeable-field list is configurable and
  release-dependent; treat as [UNCERTAIN] at detail level]
- **Reversal (FB08/F.80/FBRA):** the mechanism for a posted financial document that is wrong —
  creates the offsetting reversal document as described in §1.
- **Cancellation (void):** for SD billing documents, VF11 "Invoice Cancellation" cancels the billing
  document (creating a cancellation billing doc, type S1), which then flows a reversal FI document;
  for checks, FCH8 voids the check *and* reverses the payment document in one step; for payment
  documents generally, FBRA "resets" clearing (un-clears) so that reversal can proceed. "Cancel"
  in SAP generally means *the business transaction is annulled*, implemented under the hood by the
  same reversal mechanics plus module-specific state updates. [COMMUNITY][THIRD-PARTY]
- **Credit/debit memo vs reversal:** practitioners and SAP notes stress that a reversal is for
  *erroneous postings* (wrong entry), while a **credit/debit memo** (F-27/F-41, or SD credit memo
  requests) is for *legitimate business corrections* (price dispute, partial refund, goods return).
  Example: "use cancellations in the current period (…), in all other cases use credit/debit memo
  requests" — a cancellation (reversal-style) is only for not-yet-effective invoices; a credit memo
  is the business-correct instrument otherwise. [COMMUNITY]
- **Refund:** an actual payment back to the customer (outgoing payment F-53 / payment run) — a
  separate new payment document, never implemented as "reverse the invoice". [GENERAL — well
  established SAP behavior]

### Sources
- https://community.sap.com:443/t5/enterprise-resource-planning-q-a/reversing-a-fi-document-related-to-sd/qaa-p/12210652
  (SD billing cancellation: VF11 picks the original date; practitioner guidance "in all other
  cases they use credit/debit memo requests"; SAP note 2550414 referenced for wrong document date)
- https://www.erpgreat.com/financial/mass-void-those-wrongly-issued-checks.htm
  (FCH8 "does the void and reversal in one go"; then FBRA to reverse the payment documents —
  void ≠ reversal, void *includes* reversal of the FI posting)
- https://uperform.magic.ms.gov/ucontent/56244677ae2e4050b1dbeba8b3f01c5a_en-US/wi/pdf/index.pdf
  (VF02: change billing document vs cancel — cancellation allowed before release to accounting;
  "Interagency sales orders should not be cancelled. A credit memo must be generated to correct.")
- https://community.sap.com/t5/enterprise-resource-planning-q-a/unable-to-reverse-fi-document-in-t-code-fb08/qaq-p/9218212
  (F5673: "Reverse the original transaction" — FB08 cannot reverse a document posted outside FI)

### Informs owner questions
- **Correction vs reversal vs refund vs void:** SAP's taxonomy is operationally clean and maps
  directly onto erp-kaspi: (1) *Correction* = edit of descriptive fields on an unposted/incorrectly
  posted doc (no financial effect); (2) *Reversal* = offsetting document for an erroneous posted
  operation (financial effect in some period); (3) *Cancellation/void* = business annulment
  (check void, invoice cancel) implemented via reversal + module state change; (4) *Refund* = a new
  payment operation to the counterparty (never a reversal of the invoice). If the owner memo mixes
  these, SAP says: keep them as four distinct code paths with distinct reasons/documents.

---

## 6. Closed-period P&L protection

### Observed behavior

- **Posting-period control (OB52)** is the gatekeeper: periods are opened/closed per posting-period
  variant × account type. Posting into a closed period is *rejected*, not backdated silently.
  Closing period N after month-end activities is routine (OB52 after MM close MMPV, CO close OKP1).
  [COMMUNITY]
- **Special periods 13–16** exist for year-end audit/tax adjustments posted *after* period 12 is
  closed — postings there still belong to the old fiscal year but are made while period 12 is
  locked. [THIRD-PARTY][COMMUNITY]
- **Balance carryforward (F.16 classic G/L / FAGLGVTR new G/L):** P&L accounts carry forward their
  *balance* into the retained-earnings account in the new year; B/S accounts carry forward
  individually. Once carryforward runs and periods are closed, historical-year figures are
  effectively immutable because OB52 blocks any posting dated into the old year. [THIRD-PARTY][COMMUNITY]
- **Result for P&L:** SAP does not "freeze P&L via a status" — it freezes it by *closing the
  period*. A reversal of an old-period document lands in the *current* period (per §2), so the
  closed period's reported P&L is untouched, while the current period absorbs the correction.
  Negative postings keep account turnover clean so the current period's P&L is not distorted by
  inflated debit/credit totals. [THIRD-PARTY][COMMUNITY]

### Sources
- https://www.stechies.com/amp/steps-closing-period-mm-fi-co/ (OB52: specify time intervals,
  close old / open new periods)
- https://github.com/boxlogodev/sapstack/blob/HEAD/./plugins/sap-fi/skills/sap-fi/SKILL.md
  (posting period control: OB52 per account type A/D/K/M/S; special periods 13–16 for year-end
  audit/tax adjustments; F.16 carry forward P&L → retained earnings; FAGLGVTR in S/4HANA)
- https://www.scribd.com/document/46810632/RFSUMB00-Year-End-Closing-in-Italy-3
  (year-end closing mechanics: P&L balances posted to retained earnings; carryforward program)
- https://www.scribd.com/document/462601214/SAP-Year-End-Closing-Process
  (OB52 path; carry balances forward; number ranges for the new year)
- https://github.com/o-yol/kiit-ie_sap-capstone-project/blob/HEAD/TechNova_SAP_Blueprint/06_Process_Flows/r2r_process_flow.md
  (year-end close sequence: F.16 carry forward, OB52 open new year)

### Informs owner questions
- **Historical P&L preservation:** SAP's answer is *period locks + carryforward*, not a
  "posted-period is frozen" flag on each document. For erp-kaspi: the natural equivalent is a
  `posting_periods` control table (open/closed per period) consulted by both `post()` and
  `reverse()`; reversal date defaults to original date only when that period is open.
  This keeps the P&L query simple (statuses only) while period control guards history.

---

## 7. Concurrency / idempotency of reversal

### Observed behavior

- **SAP has no idempotency key on reversal.** Protection is **state-based**: FB08 checks the
  document's reversal state before posting — a document that already has STBLG/XREVERSAL set
  (already reversed) or that is itself a reversal document fails the precondition ("Is not a
  reversing document or already reversed"). The check and the stamp happen in the same posting
  transaction, so a duplicate reversal is rejected at the data level (enqueue locks serialize
  access to the document number — SAP uses its lock mechanism on BKPF during document changes).
  [COMMUNITY/OPERATING-DOC]
- **Reset-first discipline for cleared documents:** a cleared document cannot be reversed directly;
  FBRA first *resets* the clearing document (un-clear), then reverses. A document that is cleared
  again in the meantime fails. This is the concurrency story: reversal operates on current document
  state, re-validated at post time. [COMMUNITY]
- **Practical caveat:** the protection is only as good as everyone using FB08/FBRA (i.e., going
  through the reversal API rather than hand-posting offsetting journals). A manual F-02 journal
  that "looks like" a reversal bypasses the STBLG check entirely — which is why the SCEIS guide
  says manual offsetting entries are *possible but discouraged*: they break the cross-referencing
  audit trail. [COMMUNITY/OPERATING-DOC]
- erp-kaspi already has an idempotency-key mechanism on POST /operations (commit 86f149b,
  uq_operations_import_fingerprint). SAP's analog is weaker (state check only), so erp-kaspi's
  key-based approach is *stricter* than SAP's here — worth stating in the memo rather than
  weakening it to match SAP.

### Sources
- https://cg.sc.gov/sites/cg/files/Documents/Guidance%20and%20Forms%20for%20State%20Agencies/CGs%20Accounting%20Policies%20and%20Procedures/Accounting%20Guidance/How%20to%20Record%20Document%20Reversals%20Using%20SCEIS%20Transaction%20Code%20FB08.pdf
  (preconditions incl. "not a reversing document or already reversed"; manual offsets discouraged
  because FB08 auto-links documents)
- https://community.sap.com/t5/application-development-discussions/payment-document-reversed/td-p/6586971
  (state stored in XREVERSAL/STBLG — the same fields the precondition reads)
- https://community.sap.com/t5/enterprise-resource-planning-q-a/fbra-reversed-document-reversal-issue/qaq-p/12679926/comment-id/4129544
  (reversal-of-reversal blocked by state check)
- https://community.sap.com/t5/enterprise-resource-planning-q-a/unable-to-reverse-fi-document-in-t-code-fb08/qaq-p/9218212
  (F5673 module check at reversal time — validation re-runs at post)

### Informs owner questions
- **Idempotency/concurrency:** SAP relies on (a) re-validation of document state at post time
  inside one DB transaction + SAP enqueue locks, and (b) the STBLG/XREVERSAL stamp as the
  "already reversed" guard. For erp-kaspi this suggests: `reverse_operation()` should, in a
  single DB transaction, (1) `SELECT … FOR UPDATE` the original, (2) reject if already reversed
  or a reversal itself, (3) insert the reversal doc + stamp the original. The existing idempotency
  key on POST /operations is complementary and stricter — keep both.

---

## Uncertainties and source limitations

1. **No help.sap.com page was directly retrievable in this research** — SAP's documentation portal
   requires navigation that browser.search did not surface for the reversal topic. The claims above
   rest on SAP Community threads (many with "SAP Managed Tags", accepted solutions by practitioners)
   and operating guides written against SAP behavior. Where an official doc is essential (e.g.,
   exact FB02 changeable-field list, BAPI_ACC_DOCUMENT_REV_POST semantics), mark as needing a
   follow-up with help.sap.com or an SAP support note.
2. **Exact error message number/text for duplicate reversal** (often cited as F5-class messages)
   was not confirmed — only the precondition rule and observed "cannot be reset" behavior.
3. **Negative postings across fiscal years** (restriction to current/following year) is asserted in
   practitioner docs but not confirmed here.
4. **S/4HANA specifics beyond Fiori app names** (e.g., Universal Journal ACDOCA reversal indicators
   RVSDOCNR/ORNDOCNR — seen in ECMCA table definition) were noted but not deeply verified.
   Source: https://www.se80.co.uk/sap-tables-pre-ecc/e/ecmc/ecmca.htm (ACDOCA-side fields
   RVSDOCNR "Reverse Document Number" and ORNDOCNR "Document number of reversed document").
5. community.sap.com threads are old (2006–2023); core FI reversal behavior is stable across ECC and
   S/4HANA, but Fiori app names/flows reflect S/4HANA.

## Recommended citation block for the decision memo

> SAP (ECC and S/4HANA) reverses posted documents by posting a new offsetting reversal document
> (FB08 individual / F.80 mass / FBRA for cleared docs; Fiori "Reverse Journal Entries") — the
> original is never modified. Original and reversal are cross-linked via BKPF-STBLG/STJAH and
> flagged BKPF-XREVERSAL 1/2 (BSEG-XRAGL at line-item level). Reversal date defaults to the
> original posting date when that period is open (OB52); otherwise a reversal reason allowing an
> alternative date is required and the reversal posts into the current open period — posting into
> a closed period is rejected. Reversal reasons carry two functional flags: "negative postings"
> (same-side negative amounts instead of opposite-side postings, keeping account turnover clean)
> and "alternative posting date". A reversal document cannot itself be reversed; an erroneous
> reversal is undone by a third manual offsetting posting (F-02 "post with reference"). Duplicate
> reversal is prevented by re-checking document state at post time (already-reversed/reversing
> documents are rejected). Corrections of posted docs (FB02) are limited to non-financial header
> fields; credit/debit memos are the instrument for legitimate business corrections; refunds are
> separate payment documents. Closed-period P&L is protected by period locks (OB52) plus year-end
> balance carryforward (F.16/FAGLGVTR) into retained earnings — not by per-document status flags.
