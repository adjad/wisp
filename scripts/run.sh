#!/usr/bin/env bash
# Start the Wisp agent service (assumes oMLX is already running on :8000).
#
# Invoked via `python -m uvicorn`, not `.venv/bin/uvicorn`: console-script
# shebangs carry the venv's absolute path, so calling them directly breaks the
# moment the checkout moves. Going through the interpreter is relocation-safe.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${WISP_PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "No interpreter at $PY — run scripts/setup.sh first," >&2
  echo "or set WISP_PYTHON to the interpreter you want to use." >&2
  exit 1
fi

# --reload is a development convenience: it restarts the backend on every source
# edit, which is wrong under Wisp.app (which manages this process itself).
RELOAD=""
[ "${WISP_DEV:-0}" = "1" ] && RELOAD="--reload"

exec "$PY" -m uvicorn service.main:app \
  --host "${WISP_HOST:-127.0.0.1}" \
  --port "${WISP_PORT:-${MOE_PORT:-8765}}" \
  $RELOAD
