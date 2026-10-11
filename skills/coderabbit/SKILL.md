---
name: "coderabbit"
description: "Use CodeRabbit when the user asks for AI code review: PR reviews via the GitHub/GitLab app, local reviews via the `cr` CLI, review config (.coderabbit.yaml), or the CodeRabbit REST API (reports, metrics, learnings, roles)."
---

# CodeRabbit

## Purpose
Use CodeRabbit for AI code review automation. Primary surfaces: the Git App
(auto reviews on pull/merge requests), the `cr` CLI (local and CI reviews with
`--agent` NDJSON output for coding agents), `.coderabbit.yaml` repo config,
and the REST API (`https://api.coderabbit.ai`) for reports, metrics, learnings,
users, roles, and audit logs. There is NO documented REST endpoint that
triggers a review — triggering is done via the Git App, `@coderabbitai` PR
commands, or the CLI.

## Tooling
Add service-specific CLIs under `~/workspace/skills/coderabbit/bin/`.

The official `cr` CLI is the preferred local-review tool; wrap it rather than
reimplementing it. For REST calls, Python CLIs must import
`/opt/hatch/skills/skill-creator/bin/dynamic_credentials.py` and call
`add_surrogate_to_request(...)` (or the query/path variants) before
authenticated requests, matching where the provider reads the key
(`x-coderabbitai-api-key` header). If they use `urllib`, read JSON responses
with `read_json_response(resp)` from the same helper instead of calling
`resp.read()` directly. They must send only `hsurr:*` values, and only to the
hosts below.

## Auth
The credential is already stored; nothing here collects one. Never ask the
user to paste a raw key in chat, set a secret environment variable, pass a
secret flag, or write an auth file. API keys are created in the CodeRabbit
dashboard; the CLI can also authenticate via browser OAuth (`cr auth login`).

A 401 or 403 is a question about the request before it is a question about the
key. Check that the credential was attached at all: a request built without
the helpers named under Tooling carries nothing, and that looks exactly like a
wrong or under-scoped token. Only once a request that did carry the credential
is still rejected, call `credentials.request_api_access` with `reconnect` to
replace it. The connector is stored as `custom.coderabbit`.

## Operating Rules
1. Use this skill when the user asks for CodeRabbit or AI code review.
2. Restrict authenticated requests to: api.coderabbit.ai.
3. Do not print, log, or persist raw credentials.
4. If auth is missing or rejected, follow the Auth section rather than asking
   for a key.
5. Reviews are triggered via the Git App (automatic), `@coderabbitai` PR
   comment commands, or `cr review [--agent]` — never invent a
   "trigger review" REST endpoint.
6. Most REST endpoints require the Enterprise plan; the AI Deep Scan findings
   API works on all plans with a non-agentic key holding `security:read`.
   Check each endpoint's plan/permission requirements in
   https://docs.coderabbit.ai/api-reference/ before calling.
7. For agent consumption always prefer `cr review --agent` (NDJSON, documented
   exit codes) over scraping plain-text CLI output.
8. Repo review behavior is configured in `.coderabbit.yaml` at the repo root
   (profiles: `chill`, `quiet`, `assertive`, `default`); validate against the
   official schema before writing one.
9. Verified 2026-10-10 (official docs): base `https://api.coderabbit.ai`,
   header `x-coderabbitai-api-key`; `POST /api/v1/report.generate` for reports.
