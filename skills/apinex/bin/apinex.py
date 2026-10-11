#!/usr/bin/env python3
"""CLI for the Apinex API (OpenAI-compatible /v1), using the stored custom.apinex credential.

Never prints or logs the raw credential: auth goes through the authd surrogate exchange.
"""
import argparse
import json
import sys
import urllib.request

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import (
    add_surrogate_to_request,
    read_json_response,
    DynamicCredentialError,
)

BASE = "https://api.apinex.bond/v1"
ALLOWED_HOSTS = ["api.apinex.bond"]
CREDENTIAL = "custom.apinex"


def _authed_request(method: str, path: str, payload: dict | None = None) -> urllib.request.Request:
    url = BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    # Provider's WAF rejects the default Python-urllib UA with 403.
    req.add_header("User-Agent", "Cline/3.0 VSCode/1.90")
    if data:
        req.add_header("Content-Type", "application/json")
    add_surrogate_to_request(req, CREDENTIAL, allowed_hosts=ALLOWED_HOSTS)
    return req


def cmd_models(_args: argparse.Namespace) -> int:
    req = _authed_request("GET", "/models")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = read_json_response(resp)
    except DynamicCredentialError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for m in body.get("data", []):
        print(m.get("id"))
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    prompt = args.prompt
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            prompt = f.read()
    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": args.model,
        "messages": messages,
    }
    if args.max_tokens:
        payload["max_tokens"] = args.max_tokens
    req = _authed_request("POST", "/chat/completions", payload)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = read_json_response(resp)
    except DynamicCredentialError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        print(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError):
        print(json.dumps(body, ensure_ascii=False)[:2000])
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="apinex")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("models", help="List available model IDs")

    chat_p = sub.add_parser("chat", help="Chat completion")
    chat_p.add_argument("--model", default="glm-5.3", help="Model ID (see `models`; default: glm-5.3)")
    chat_p.add_argument("--prompt", default="", help="User prompt")
    chat_p.add_argument("--prompt-file", default="", help="Read prompt from file")
    chat_p.add_argument("--max-tokens", type=int, default=0)
    chat_p.add_argument("--system", default="", help="System prompt")

    args = parser.parse_args()
    if args.cmd == "models":
        return cmd_models(args)
    return cmd_chat(args)


if __name__ == "__main__":
    sys.exit(main())
