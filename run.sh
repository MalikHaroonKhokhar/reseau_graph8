#!/usr/bin/env bash
# Runs the gateway (8080), its tunnel and the dashboard (http://127.0.0.1:8081). Ctrl-C stops all three.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
for port in 8080 8081; do
  if lsof -tiTCP:$port -sTCP:LISTEN >/dev/null; then echo "port $port is already in use: stop the old gateway/dashboard first" >&2; exit 1; fi
done
trap 'kill 0' EXIT   # background jobs ignore Ctrl-C in a script, so stop them ourselves

uv run python -m reseau.front --port 8080 &
uv run python -m reseau.tunnel --port 8080 &
uv run python -m reseau.dashboard --port 8081 &
wait
