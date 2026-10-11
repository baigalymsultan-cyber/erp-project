---
name: "betterstack"
description: "Use Better Stack when the user asks for Better Stack, uptime monitoring, heartbeats, incidents, status pages, or log queries."
---

# Better Stack

## Purpose
Operate Better Stack programmatically: uptime monitors, heartbeat checks,
incidents (list/acknowledge/resolve), status pages, and log queries. Read-mostly
by default; writes (create monitor, ack/resolve incident, post status update)
only on explicit user request.

## Tooling
Add service-specific CLIs under `~/workspace/skills/betterstack/bin/`.

Python CLIs must import `/opt/hatch/skills/skill-creator/bin/dynamic_credentials.py`
and call `add_surrogate_to_request(...)`, `url_with_surrogate_query_param(...)`, or
`url_with_surrogate_path_segment(...)` before authenticated requests, matching where
the provider reads the key. Better Stack reads the token from the
`Authorization: Bearer` header, so attach the surrogate there. If they use
`urllib`, read JSON responses with `read_json_response(resp)` from the same
helper instead of calling `resp.read()` directly. They must send only `hsurr:*`
values, and only to the hosts below. Always send a client-like `User-Agent`.
No official CLI exists; there is a first-party Terraform provider.

## Auth
No credential is stored yet — nothing here collects one. Never ask the user to
paste a raw key in chat, set a secret environment variable, pass a secret flag,
or write an auth file. The token is created by the user at
betterstack.com → Settings → API tokens: a **Global API token** (all teams) or
team-scoped **Uptime** / **Telemetry** tokens. Once stored via the approved
secure setup flow, use it as `Authorization: Bearer <token>`.

Token scope notes (official): Uptime tokens manage monitors, heartbeats,
incidents, status pages, policies, on-calls. Telemetry tokens manage log
sources; the SQL query API uses separate username/password connections
(Integrations → Connect ClickHouse HTTP client), not the Bearer token.
Source tokens are ingestion-only and must never be used for management calls.

A 401 or 403 is a question about the request before it is a question about the
key. Check that the credential was attached at all: a request built without
the helpers named under Tooling carries nothing, and that looks exactly like a
wrong or under-scoped token. Only once a request that did carry the credential
is still rejected, ask the user to check the token scope (global vs
team-scoped, Uptime vs Telemetry) or regenerate it.

## Operating Rules
1. Use this skill when the user asks for Better Stack or its products
   (uptime, heartbeats, incidents, status pages, logs).
2. Restrict authenticated requests to: `uptime.betterstack.com`,
   `incidents.betterstack.com`, `telemetry.betterstack.com`. The public
   status page JSON (`{status_page_url}/index.json`) needs no auth.
3. Do not print, log, or persist raw credentials or source tokens.
4. Writes are explicit-opt-in per action: creating/pausing monitors,
   acknowledging or resolving incidents, posting status page updates. Reads
   (list monitors/heartbeats/incidents, SLA summaries, log queries, public
   status JSON) are free.
5. All management APIs follow JSON:API. Incidents list/ack/resolve live under
   `/api/v3`; most other Uptime resources under `/api/v2`; heartbeat ingest
   under `/api/v1/heartbeat/{token}`. Do not mix versions.
6. Log queries go through the SQL API (ClickHouse HTTP, Basic auth
   connection), not the Bearer REST API. The legacy `GET /api/v1/query`
   endpoint is decommissioned — never use it.
7. If auth is missing or rejected, follow the Auth section rather than asking
   for a key.
8. Verified 2026-10-10 (official docs): `GET /api/v2/monitors`,
   `GET /api/v2/monitors/{id}/sla`, `GET /api/v2/heartbeats`,
   `GET /api/v3/incidents`, `POST /api/v2/incidents` on the hosts above.
