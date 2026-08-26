#!/usr/bin/env bash
# Start the MOE agent service (assumes oMLX is already running on :8000).
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/uvicorn service.main:app --port "${MOE_PORT:-8765}" --reload
