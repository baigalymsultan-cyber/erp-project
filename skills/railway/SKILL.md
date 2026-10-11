# Railway

Use Railway when the user asks for Railway, railway.com / railway.app, deploying to Railway, or managing Railway projects/services/deployments. Railway is the deploy target for erp-kaspi (Hurst's choice, 2026-10-10).

## What this covers

Railway's public GraphQL API — the same API that powers the Railway dashboard. Use it to create projects, manage services, set variables, trigger deployments, and check deploy status. All operations are POSTs to a single endpoint.

**Out of scope:** the `railway` CLI (browser-OAuth login; not usable from this VM).

## Authentication

- **Endpoint:** `https://backboard.railway.com/graphql/v2` (single endpoint, every operation is a POST)
- **Header:** `Authorization: Bearer <token>`
- **Token types:** personal (account) tokens — access to all resources; team tokens — scoped to one team. Create at `https://railway.com/account/tokens` (Account settings → Tokens).
- **Stored credential:** `custom.railway` via Secure Vault (`bearer_header` placement). Use the `dynamic_credentials` helper (`add_surrogate_to_request`) — never handle the raw token.

## API shape

- Every request: POST with JSON body `{ "query": "...", "variables": { ... } }`, `Content-Type: application/json`.
- Connection types use cursor pagination: `edges { node { ... } }`.
- When unsure of a field name, run an introspection query first — never guess schema.

## erp-kaspi deploy notes (2026-10-10)

- Target: two services in one Railway project — `api` (FastAPI, `uvicorn app.api:app :8000`, healthcheck `/health`) and `frontend` (Streamlit).
- Database: Neon project `erp-kaspi` (`green-band-80948116`) — Railway services connect via `DATABASE_URL`, no Railway Postgres needed.
- Frontend talks to the API only through `ERP_API_BASE_URL` (see `frontend/api_client.py`).
- Required env: `DATABASE_URL` (Neon), `APP_ENV=production` (requires `RATE_LIMIT_SHARED=1`), `APP_JWT_SECRET` (generate), `ERP_API_BASE_URL` (for frontend service).
- Railway builds Python services zero-config from the repo; a `Dockerfile` is available as fallback.
- Sequencing (Hurst's call): deploy after `arch-refactor-001` push + CI green + merge decision, OR deploy the branch as-is — his one-line call.

## Sources

- Official: https://docs.railway.com/reference/public-api (endpoint, token types, auth)
- Token creation: https://railway.com/account/tokens
