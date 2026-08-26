#!/usr/bin/env bash
# One-command sandbox launcher: a second, isolated Wisp backend (own
# ~/.wisp-sandbox/moe) plus the sandbox server (fake Swift app + iPhone
# mockup + Wisp Dev), talking to each other on non-default ports.
#
# Deliberately NOT port 8765 / the real ~/.moe: WispClient.swift hardcodes
# 8765 and PortGuard.swift SIGTERMs anything else bound to it, and if the
# real Wisp.app were left running while this proxied to 8765, both would be
# subscribers on the same /assistant/events hub — the real app could
# receive and execute a send_email/send_message the sandbox agent decided
# to send, from the user's real accounts. The guard below exists so that
# can never happen by accident.
set -euo pipefail
cd "$(dirname "$0")/.."

export WISP_SANDBOX_HOME="${WISP_SANDBOX_HOME:-$HOME/.wisp-sandbox}"
export WISP_HOME="${WISP_HOME:-$WISP_SANDBOX_HOME/moe}"
BACKEND_PORT="${WISP_BACKEND_PORT:-8775}"
export WISP_BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
SANDBOX_PORT="${SANDBOX_PORT:-8766}"

resolved_wisp_home="$(python3 -c "import os; print(os.path.realpath(os.path.expanduser(os.environ['WISP_HOME'])))")"
resolved_real_home="$(python3 -c "import os; print(os.path.realpath(os.path.expanduser('~/.moe')))")"
if [ "$resolved_wisp_home" = "$resolved_real_home" ]; then
  echo "refusing to run the sandbox against your real ~/.moe (WISP_HOME resolved to it)" >&2
  exit 1
fi

mkdir -p "$WISP_HOME" "$WISP_SANDBOX_HOME"

if lsof -nP -iTCP:"$BACKEND_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "port $BACKEND_PORT is already in use — set WISP_BACKEND_PORT to pick another" >&2
  exit 1
fi
if lsof -nP -iTCP:"$SANDBOX_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "port $SANDBOX_PORT is already in use — set SANDBOX_PORT to pick another" >&2
  exit 1
fi

if ! curl -sf http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
  echo "⚠ oMLX doesn't answer on :8000 — /agent turns will fail until it's running" >&2
fi

echo "sandbox home:   $WISP_SANDBOX_HOME"
echo "wisp home:      $WISP_HOME"
echo "backend:        http://127.0.0.1:$BACKEND_PORT"
echo "sandbox server: http://127.0.0.1:$SANDBOX_PORT"

.venv/bin/uvicorn service.main:app --port "$BACKEND_PORT" >"$WISP_SANDBOX_HOME/backend.log" 2>&1 &
BACKEND_PID=$!

cleanup() {
  kill "$BACKEND_PID" "$SANDBOX_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo -n "waiting for the sandbox backend to come up"
for _ in $(seq 1 100); do
  if curl -sf "$WISP_BACKEND_URL/mode" >/dev/null 2>&1; then
    echo " ready"
    break
  fi
  echo -n "."
  sleep 0.3
done
if ! curl -sf "$WISP_BACKEND_URL/mode" >/dev/null 2>&1; then
  echo
  echo "backend never came up — see $WISP_SANDBOX_HOME/backend.log" >&2
  exit 1
fi

.venv/bin/uvicorn sandbox.server:app --port "$SANDBOX_PORT" --reload \
  >"$WISP_SANDBOX_HOME/sandbox.log" 2>&1 &
SANDBOX_PID=$!

sleep 1
open "http://127.0.0.1:$SANDBOX_PORT/" 2>/dev/null || true

wait
