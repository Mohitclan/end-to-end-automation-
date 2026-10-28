#!/usr/bin/env bash
# Start everything for local testing: Redis (port 6390), the worker, and the web app.
# Ctrl+C stops all three. Run from anywhere: ./scripts/dev.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REDIS_PORT=6390
REDIS_BIN="${REDIS_BIN:-dev-data/bin/redis-server}"
command -v redis-server >/dev/null 2>&1 && [ ! -x "$REDIS_BIN" ] && REDIS_BIN="$(command -v redis-server)"

if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and set SECRET_KEY first." >&2
  exit 1
fi

WEB_PORT=8000
if lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $WEB_PORT is already in use (is growthstack already running?):" >&2
  lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN >&2
  echo "Stop that process first, e.g.: kill <PID>" >&2
  exit 1
fi

pids=()
cleanup() {
  echo; echo "Stopping..."
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if dev-data/bin/redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1; then
  echo "Redis already running on port $REDIS_PORT"
else
  if [ ! -x "$REDIS_BIN" ]; then
    echo "Redis not found. Install it (brew install redis) or put redis-server in dev-data/bin/." >&2
    exit 1
  fi
  "$REDIS_BIN" --port "$REDIS_PORT" --bind 127.0.0.1 --save "" --appendonly no --dir dev-data --loglevel warning &
  pids+=($!)
  echo "Started Redis on port $REDIS_PORT"
fi

.venv/bin/growthstack migrate
.venv/bin/growthstack worker &
pids+=($!)
echo "Started worker"
echo "Web app: http://localhost:$WEB_PORT"
.venv/bin/growthstack web --port "$WEB_PORT"
