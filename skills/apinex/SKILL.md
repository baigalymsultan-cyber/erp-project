---
name: "apinex"
description: "Use Apinex when the user asks for Apinex or this provider's API."
---

# Apinex

## Purpose
Use Apinex with the user-connected `custom.apinex` credential. Default working model: `glm-5.3` (user's choice 2026-10-09; was `gpt-6-astra`; `claude-opus-5.5` is blocked by provider billing until account top-up).

## Tooling
Add service-specific CLIs under `~/workspace/skills/apinex/bin/`.

Python CLIs must import `/opt/hatch/skills/skill-creator/bin/dynamic_credentials.py` and call `add_surrogate_to_request(...)`, `url_with_surrogate_query_param(...)`, or `url_with_surrogate_path_segment(...)` before authenticated requests, matching where the provider reads the key. If they use `urllib`, read JSON responses with `read_json_response(resp)` from the same helper instead of calling `resp.read()` directly. They must send only `hsurr:*` values, and only to the hosts below.

## Auth
The credential is already stored; nothing here collects one. Never ask the user to paste a raw key in chat, set a secret environment variable, pass a secret flag, or write an auth file.

A 401 or 403 is a question about the request before it is a question about the key. Check that the credential was attached at all: a request built without the helpers named under Tooling carries nothing, and that looks exactly like a wrong or under-scoped token. Only once a request that did carry the credential is still rejected, call `credentials.request_api_access` with `reconnect` to replace it. The connector is stored as `custom.apinex`.

## Operating Rules
1. Use this skill when the user asks for Apinex or this provider's API.
2. Restrict authenticated requests to: api.apinex.bond.
3. Do not print, log, or persist raw credentials.
4. If auth is missing or rejected, follow the Auth section rather than asking for a key.
5. Always send a client-like `User-Agent` (e.g. `Cline/3.0 VSCode/1.90`): the provider's WAF answers 403 to the default Python-urllib UA.
6. Model notes (verified 2026-10-08): the user prefers `claude-opus-5.5`, but the provider returns **402** for all Claude models until the apinex account has at least one balance top-up (`billing_error`: "Anthropic models (Claude) are only available to accounts with at least one balance top-up"). `gpt-6-astra` is verified working on the current key. Model IDs come from `GET /v1/models` (36 listed, incl. `free/*` tier).
