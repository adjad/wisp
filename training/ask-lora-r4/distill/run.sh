#!/bin/bash
# Run gen.py (or GEN=vision_gen.py) under the heavy lock, with oMLX's per-model settings snapshotted and restored on any exit.
#   [GEN=vision_gen.py] run.sh <its args...>
# Needs: Wisp quit (8765/8791 not listening), oMLX running on $OMLX_BASE, the heavy lock free.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-$HOME/Desktop/MOE_Project/.venv/bin/python}"
OMLX_BASE="${OMLX_BASE:-http://127.0.0.1:8000}"
GEN="${GEN:-gen.py}"
LOCK="${LOCK:-/private/tmp/claude-$(id -u)/wisp-heavy.lock}"
SETTINGS="${OMLX_SETTINGS:-$HOME/.omlx/model_settings.json}"
# kept beside the scripts (gitignored), not in $TMPDIR: a quit app can wipe $TMPDIR before the trap runs
mkdir -p "$HERE/_build"
SNAP="${SNAP:-$HERE/_build/model_settings.before.$$.json}"

if lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1; then echo "WISP IS RUNNING - not starting"; exit 4; fi
# Inside full.sh, which holds the lock (and the settings snapshot) for the whole run: just run.
if [ -n "${HELD_LOCK:-}" ] && [ "$HELD_LOCK" = "$LOCK" ] && [ -d "$LOCK" ]; then
  OMLX_BASE="$OMLX_BASE" exec "$PY" -u "$HERE/$GEN" "$@"
fi
mkdir "$LOCK" 2>/dev/null || { echo "LOCK HELD: $(cat "$LOCK/owner" 2>/dev/null)"; exit 3; }
echo "ask-lora r4 distill $GEN pid $$ $(date)" > "$LOCK/owner"

cp "$SETTINGS" "$SNAP" || { echo "cannot snapshot $SETTINGS"; rm -rf "$LOCK"; exit 2; }
restore() {
  cp "$SNAP" "$SETTINGS"
  curl -s -m60 -X POST "$OMLX_BASE/admin/api/reload" >/dev/null
  echo "== $(date) oMLX settings restored and reloaded"
  rm -rf "$LOCK"
  echo "== lock released"
}
trap restore EXIT
trap 'exit 130' INT TERM HUP

OMLX_BASE="$OMLX_BASE" "$PY" -u "$HERE/$GEN" "$@"
