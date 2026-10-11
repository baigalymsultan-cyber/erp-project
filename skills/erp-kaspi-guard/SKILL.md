---
name: "erp-kaspi-guard"
description: "Financial-invariant guardrails for the erp-kaspi ERP project. Use before ANY code change in ~/workspace/erp-kaspi: blocked areas, immutable semantics, verification gates, and the post-change protocol."
metadata: { "includeInPrompt": true }
---

# erp-kaspi-guard

## Purpose

Prevent regressions and semantic violations in the erp-kaspi financial ERP
(Python 3.11+, FastAPI, PostgreSQL, SQLAlchemy 2.x, Alembic, Streamlit).
Read this skill before planning or implementing any change in
`~/workspace/erp-kaspi`. It encodes standing owner constraints that must
never be violated by autonomous work.

## Hard Blockers (do NOT touch without owner decision)

| Blocker | Scope | Rule |
|---|---|---|
| `REVERSAL_Q1_Q7` | `reverse_operation()`, ADR-016, reversal statuses/types, `related_operation_id`, P&L reversal semantics, TD-018, TD-019 steps 1/3 | No code changes. Research + docs only. Memo: `docs/REVERSAL_DECISION_MEMO.md` |
| `KASPI_OQ_003` | Kaspi API contract | Only official Kaspi docs (`guide.kaspi.kz`, `kaspipay.kz`). Never invent endpoints/fields/schemas |
| `REPORT_8_DEFINITION` | 8th report | 7 reports + Excel exist. Never invent the 8th |
| `TD-020` | Multi-employee payroll PAID lifecycle | `pay_payroll_summary()` period semantics frozen. Research + docs only |

A BLOCKED business decision blocks only its area — never the whole project.

## Immutable Semantics (never change or invent)

- `REVERSAL_Q1_Q7`, `KASPI_OQ_003`, `REPORT_8_DEFINITION`, TD-020 lifecycle
- PostgreSQL is the financial-invariants layer; never rewrite posted financial history
- No second auth/i18n/accounting architecture; RU/KZ/EN for new user strings
- Never invent financial/tax/accounting/payroll/Kaspi semantics

## Closed Work (do not touch without proven regression)

Transfer checkpoint `b9dd921`, TD-015, TD-016, TEST-ISOLATION-001, TD-019 step 2.
If a test/scanner reveals a real regression: investigate → fix → regression → verify.

## Verification Gates (after every meaningful change)

1. Targeted tests for the change (TDD first)
2. Full pytest: `502 passed, 1 skipped` is the baseline (2026-10-03)
3. `ruff check`, project mypy gate, `git diff --check`
4. Alembic/schema checks when DB changed; security scanner for security-sensitive changes
5. Commit with factual HEAD state; never claim PASS without actual output

## Operating Rules

1. Evidence over claims: no `PASS`/`VERIFIED` without tool output.
2. Subagent output is not proof — independently verify every delegated result.
3. Pre-existing mypy issues are classified separately, never declared fixed.
4. Scope discipline: authorized run = erp-kaspi only; ignore unrelated noise.
5. After each task: re-read the Master Prompt, pick the next safe unresolved item.
