#!/usr/bin/env bash
# One command: empty PostgreSQL 15 + the full CI gate + FIN-OPS-0001 checks.
# Requires only Docker (Desktop on Windows/macOS). Exit code = gate result.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
COMPOSE=(docker compose -f docker/gate/docker-compose.gate.yml)
trap '"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true' EXIT
"${COMPOSE[@]}" up --build --abort-on-container-exit --exit-code-from gate
