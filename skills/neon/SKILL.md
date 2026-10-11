---
name: "neon"
description: "Use Neon when the user asks for Neon or its serverless Postgres API: projects, branches, databases, connection strings, snapshots/PITR, compute endpoints."
---

# Neon

## Purpose
Use Neon with the user-connected `custom.neon` credential. Neon is a serverless
Postgres platform (separated storage/compute, autoscaling, scale-to-zero,
copy-on-write branching). The Management API covers projects, branches,
databases, roles, compute endpoints, snapshots/PITR, and connection strings —
everything the Neon Console can do.

## Tooling
Add service-specific CLIs under `~/workspace/skills/neon/bin/`.

Python CLIs must import `/opt/hatch/skills/skill-creator/bin/dynamic_credentials.py` and call `add_surrogate_to_request(...)`, `url_with_surrogate_query_param(...)`, or `url_with_surrogate_path_segment(...)` before authenticated requests, matching where the provider reads the key. If they use `urllib`, read JSON responses with `read_json_response(resp)` from the same helper instead of calling `resp.read()` directly. They must send only `hsurr:*` values, and only to the hosts below.

The official `neon` CLI (`npm install -g neon`, legacy name `neonctl`) is preferred for terminal workflows when installed; use the REST API directly for programmatic access.

## Auth
The credential is already stored; nothing here collects one. Never ask the user to paste a raw key in chat, set a secret environment variable, pass a secret flag, or write an auth file.

Pass the key as `Authorization: Bearer <key>` on every request to
`https://console.neon.tech/api/v2/`. Key types: Personal (all user projects),
Organization (whole org), Project-scoped (single project) — the stored key's
scope determines what operations are allowed.

A 401 or 403 is a question about the request before it is a question about the key. Check that the credential was attached at all: a request built without the helpers named under Tooling carries nothing, and that looks exactly like a wrong or under-scoped token. Only once a request that did carry the credential is still rejected, call `credentials.request_api_access` with `reconnect` to replace it. The connector is stored as `custom.neon`.

## Operating Rules
1. Use this skill when the user asks for Neon or its Postgres/API.
2. Restrict authenticated requests to: console.neon.tech.
3. Do not print, log, or persist raw credentials. Connection URIs returned by
   `GET /projects/{id}/connection_uri` contain passwords — treat them as secrets.
4. If auth is missing or rejected, follow the Auth section rather than asking for a key.
5. Respect rate limits: 700 req/min, 40 req/s burst per route. On HTTP 429, back off and retry.
6. Destructive operations (delete project/branch, restore/finalize_restore) require the user's explicit go-ahead naming the target. Restores are async — poll the Operations resource and do not connect until complete.
7. Key endpoints: `GET/POST /projects`, `GET/POST /projects/{id}/branches`, `POST /projects/{id}/branches/{bid}/databases`, `GET /projects/{id}/connection_uri?branch_id=&database_name=&role_name=&pooled=`, `GET /projects/{id}/snapshots`, `POST /projects/{id}/snapshots/{sid}/restore`, `POST /projects/{id}/endpoints/{eid}/start`.
8. Verified 2026-10-10 (official docs): base `https://console.neon.tech/api/v2/`; OpenAPI at `https://neon.com/api_spec/release/v2.json`; api-docs.neon.tech is deprecated in favor of neon.com/docs/reference/api.
