# ERP for Kazakhstan Small Business

An ERP built for the way small businesses in Kazakhstan actually operate:
Kaspi Shop / Kaspi Pay, orders, finance, and piece-rate payroll for
furniture production.

Two things live in this repository: the ERP codebase and a set of
authored agent skills used to build and maintain it.

## Scope

- **Kaspi Shop / Kaspi Pay** — order intake and payment flow
- **Orders** — lifecycle from intake to fulfilment
- **Finance** — ledger, receivables, reconciliation
- **Piece-rate payroll** — output-based wages for furniture production
- **Agent skills** — 8 authored skills used as build tooling

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI |
| Database | PostgreSQL 15+, SQLAlchemy 2.x, Alembic |
| Frontend | Streamlit, Plotly |
| Deploy | Railway (`erp-api`, `erp-frontend`) |

## Layout

```
erp-kaspi/    ERP codebase
skills/       8 authored skills: neon, railway, coderabbit,
              betterstack, snyk, arena, apinex, erp-kaspi-guard
prompts/      Project_PromPt.txt (active master prompt)
              FINANCE-OPS-001.txt (archived baseline)
```

## Branches

| Branch | Status | Notes |
|---|---|---|
| `main` | stable | `erp-kaspi` on top of `arch-refactor-001`, **646/646 tests pass** on a real PostgreSQL instance |
| `reversal-impl-001` | **proposed** | Compensating reversal model, migration `0024`, 14/15 tests. Awaiting confirm/override on Q1–Q7 |
| `td020-impl-001` | **proposed** | Derived `PAID` period status, 6/6 tests. Awaiting confirm/override on TD-020 |

`main` is the only branch intended to be run as-is. The two `proposed`
branches are reviewable but open.

## Tests

The suite runs against a real PostgreSQL instance, not mocks — schema
constraints, migrations and transactional behaviour are all exercised.
`main` is at 646/646.

```bash
pytest
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

Requires a reachable PostgreSQL 15+ instance and its connection string in
the environment.

## Repository caveats

- **No commit history.** Trees were uploaded through the GitHub API, so
  history reads flat. Full history lives in `erp-kaspi-branches.bundle`,
  passed separately.
- `database/schema.sql` on the `reversal` branch exceeds the GitHub API
  file-size limit. All schema changes for that branch are in migration
  `0024_reversal_model` instead.
- Deliberately not committed: unpacked skill archives (`skills-new/`,
  `ecc-skills/`), installed clients, `.venv`, caches.

## Open items

- CI on `arch-refactor-001`: triggered from a local machine into GitHub
  Actions
- Confirm/override pending on reversal Q1–Q7 and on TD-020
- Railway deployment prepared, not yet serving traffic

## Status

Active development, pre-production. Kaspi credentials and environment
secrets are kept out of the repository.

## Contact

Baigalym Sultan — Nazarbayev University
[Telegram] · [email]
