---
name: "snyk"
description: "Use Snyk when the user asks to scan for vulnerabilities, run snyk test/code/container/iac, triage Snyk findings, or query the Snyk REST API."
---

# Snyk

## Purpose
Developer security scanning for the erp-kaspi repo (Python/FastAPI): dependency
vulnerabilities (`snyk test`), SAST over first-party code (`snyk code test`),
container image scanning (`snyk container test`), and IaC misconfigurations
(`snyk iac test`). Read-only by default: scan and report; `snyk fix` and
`snyk ignore` change the repo and need explicit user approval per finding.

## Tooling
Snyk CLI installed via npm (`npm install snyk@latest -g`) or the standalone
binary from `https://static.snyk.io/cli/latest/snyk-linux`. Verify with
`snyk --version`. Add service CLIs under `~/workspace/skills/snyk/bin/`.

Python CLIs must import
`/opt/hatch/skills/skill-creator/bin/dynamic_credentials.py` and call
`add_surrogate_to_request(...)`, `url_with_surrogate_query_param(...)`, or
`url_with_surrogate_path_segment(...)` before authenticated requests, matching
where the provider reads the key. If they use `urllib`, read JSON responses
with `read_json_response(resp)` from the same helper instead of calling
`resp.read()` directly. They must send only `hsurr:*` values, and only to the
hosts below.

## Auth
The credential is stored as `custom.snyk`; nothing here collects one. Never ask
the user to paste a raw key in chat, set a secret environment variable, pass a
secret flag, or write an auth file.

CLI: prefer the `SNYK_TOKEN` env var (recognized automatically by all `test`
commands; this is the CI/CD-recommended form). `snyk auth` (browser OAuth) is
for interactive use only — never start it unprompted. REST API: send
`Authorization: token <API_TOKEN>` (lowercase `token`), `?version=2024-10-15`
on every request, `Content-Type: application/vnd.api+json` on requests with a
body. Tokens are region-specific: a token from one region returns 401 against
another region's base URL.

A 401 or 403 is a question about the request before it is a question about the
key. Check that the credential was attached at all, and for `--report` that the
token has the **View Project Ignores** permission (missing it yields 403).
Only once a request that did carry the credential is still rejected, call
`credentials.request_api_access` with `reconnect` to replace it. The connector
is stored as `custom.snyk`.

## Operating Rules
1. Use this skill when the user asks for Snyk or a vulnerability scan.
2. Restrict CLI/API use to: `*.snyk.io` (hosts: `api.snyk.io`, `api.us.snyk.io`,
   `api.eu.snyk.io`, `api.au.snyk.io`, `app.snyk.io`, `static.snyk.io`).
3. Do not print, log, or persist raw tokens.
4. If auth is missing or rejected, follow the Auth section rather than asking
   for a key.
5. Read-only default: `test` / `code test` / `container test` / `iac test`
   (optionally `--report` to push a snapshot). `snyk fix`, `snyk ignore`, and
   `snyk monitor` mutate state — each needs the user's explicit approval.
6. Exit-code contract: `0` = clean, `1` = findings (normal signal, not a tool
   failure), `2`/`3`/`69`/`75`/`77` = tool failure — re-run with `-d` or report
   the failure, never present it as a scan result.
7. Prefer `--severity-threshold=high` for triage noise control and
   `--sarif-file-output=` / `--json-file-output=` for machine-readable results
   that other tools can consume.
8. REST API: always pass `?version=` (use `2024-10-15` unless the task needs
   otherwise), follow `links.next` cursors for pagination, and back off on
   `429` (limit: 1620 req/min per API key).
9. Verified 2026-10-10 (official docs): install via npm or
   `https://static.snyk.io/cli/latest/snyk-linux`; REST bases per region
   (`https://api.snyk.io/rest`, `api.us/eu/au.snyk.io/rest`); key endpoints
   `GET /rest/orgs`, `GET /rest/orgs/{org_id}/projects`,
   `GET /rest/orgs/{org_id}/issues`.
