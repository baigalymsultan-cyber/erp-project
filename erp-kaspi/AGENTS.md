# AGENTS.md — erp-kaspi

ERP for small business in Kazakhstan (Kaspi Shop/Pay, finance, orders, furniture
production, piecework payroll).

Stack: Python 3.11+, FastAPI, PostgreSQL 15+, SQLAlchemy 2.x, Alembic.
Frontend: Streamlit + Plotly. Tests: pytest. Lint: ruff. Types: mypy.

Read `~/workspace/skills/erp-kaspi-guard/SKILL.md` before ANY code change.
It is mandatory, not reference.

## Branches

- `fin-ops-0001` — base branch.
- `arch-refactor-001` (@`86aabbb`) — mechanical refactor, 646/646 PASS on real
  PostgreSQL 16. DO NOT MERGE autonomously. Do not claim tests passed until
  CI proves it. CI green → merge-recommendation report; CI red → fix causes
  one by one, never weaken tests.
- `td020-impl-001` (@`6bef91e`) — PROPOSED: derived period PAID status.
  Needs owner confirm/override. Do not merge without it.
- `reversal-impl-001` (@`5379446`) — PROPOSED: compensating reversal model
  (ADR-016, migration 0024). 14/15 PASS. Needs owner confirm/override on
  Q1–Q7. Do not merge without it.

## Hard blockers — do NOT touch code without owner decision

| Area | Rule |
|---|---|
| Reversal Q1–Q7 (`reverse_operation()`, ADR-016, reversal statuses/types, P&L reversal semantics) | Research + docs only. Memo: `docs/REVERSAL_DECISION_MEMO.md` |
| TD-020 (multi-employee payroll PAID lifecycle) | `pay_payroll_summary()` period semantics frozen. Research + docs only |
| KASPI_OQ_003 (Kaspi API contract) | Only official Kaspi docs. Never invent endpoints/fields/schemas |
| REPORT_8 | 7 reports + Excel exist. Never invent the 8th report |

A blocked decision blocks only its area — never the whole project.

## Immutable semantics — never change or invent

- PostgreSQL is the financial-invariants layer. Never rewrite posted financial history.
- Ledger immutability holds; transfer atomicity holds.
- `v_ip_financial_summary` and `pnl()` exclude non-POSTED statuses.
- No second auth/i18n/accounting architecture. RU/KZ/EN for new user strings.
- Never invent financial/tax/accounting/payroll/bank semantics. Commissions arrive
  as facts from statements — no hardcoded rates in code.
- Bank adapters stay separate per bank (Kaspi / Halyk / FreedomPay / FreedomBank);
  never merge different banks' semantics into one artificial API.

## Closed work — do not touch without proven regression

Transfer checkpoint `b9dd921`, TD-015, TD-016, TEST-ISOLATION-001, TD-019 step 2,
accrual foundation, stabilization work. If a test/scanner reveals a real
regression: investigate → fix → regression → verify.

## Verification gates — after every meaningful change

1. Targeted tests for the change (TDD first).
2. Full pytest suite; ruff; mypy (pre-existing issues classified separately,
   never declared fixed without changing their cause).
3. `git diff --check`; Alembic/schema checks when DB changed; security scanner
   for security-sensitive changes.
4. Commit/checkpoint with the actual HEAD state.
5. Evidence over claims: never declare PASS without actual verification output.
   Never call a failure "pre-existing" without proof (reproduce on prior commit).

## Git discipline

- Forbidden without explicit owner permission: `git reset --hard`, `git clean`,
  force-push, autonomous merge of financial branches.
- Anything that sounds destructive (`drop`/`delete`/`remove` in migration,
  commit, or command names): announce beforehand in one plain sentence — what
  changes, what is untouched, how to roll back.
- Never weaken existing tests or delete a failing test to reach GREEN.

## Operating rules

- Subagent output is not proof — independently verify every delegated result.
  Each subagent gets TASK, SCOPE, FILES, CONSTRAINTS, EXPECTED OUTPUT.
- Scope discipline: authorized run = erp-kaspi only (Master Prompt /
  FINANCE-OPS-001). Ignore unrelated noise.
- Owner questions: deep analysis, take a position —
  "my decision is X because [reasons]; override me if your practice differs".
  Never an option menu with no position. Don't bake business semantics in silently.
- Credentials, OAuth, tokens, account actions: only with the owner's explicit
  step-by-step consent, stated up front — even under "no questions" orders.
  Background work must NEVER delete, revoke, or rotate credentials.
- After each task: re-read the Master Prompt, pick the next safe unresolved item.
