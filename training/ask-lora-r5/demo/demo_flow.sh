#!/bin/zsh
# Demo-flow check on an ISOLATED Wisp backend (8775) + synthetic-sources sandbox (8766).
#   CAND=gemma-4-E4B-it-r5-step-01800-oQ4e-mtp BASE=gemma-4-E4B-it-oQ4e-mtp ./demo_flow.sh
# Needs the operator's OK first: it loads the model(s) in the shared oMLX engine on :8000.
# Never uses 8765. Kills only the PIDs it started. Does not touch the Wisp app.
#   env: CAND BASE (set BASE= empty to skip) OUT REPO PY ANALYZE=1 (also tap Analyze now)

HERE="${0:A:h}"
CAND="${CAND:-gemma-4-E4B-it-r5-step-01800-oQ4e-mtp}"
BASE="${BASE-gemma-4-E4B-it-oQ4e-mtp}"
REPO="${REPO:-/Users/adijain/Desktop/MOE_Project/.claude/worktrees/r5-default-model}"
PY="${PY:-/Users/adijain/Desktop/MOE_Project/.venv/bin/python}"
R3="${R3:-/Users/adijain/Desktop/MOE_Project/.claude/worktrees/ask-lora-brev-2caec5/training/ask-lora-r3}"
IMAGE="${IMAGE:-$R3/gate/vision/images/err_mail_size.png}"
WAV="${WAV:-$R3/audio/clips/aud_command_001.wav}"
OUT="${OUT:-$HOME/Desktop/r5-pilot/demo-flow-$(date +%Y%m%d-%H%M%S)}"
BACKEND_PORT=8775
SANDBOX_PORT=8766
GEMMA_IDS=("gemma-4-E4B-it-oQ4e-mtp" "gemma-4-E4B-it-r5-step-01800-oQ4e-mtp")

[ -d "$REPO" ] || { echo "REPO not found: $REPO" >&2; exit 1; }
[ -x "$PY" ] || { echo "python not found: $PY" >&2; exit 1; }
[ -f "$IMAGE" ] || { echo "image not found: $IMAGE" >&2; exit 1; }
[ -f "$WAV" ] || { echo "wav not found: $WAV" >&2; exit 1; }

BACKEND_PID=""
SANDBOX_PID=""
WORK=""

listeners() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null; }

stop_pid() {  # by PID only
  local p="$1" i
  [ -n "$p" ] || return 0
  kill -0 "$p" 2>/dev/null || return 0
  kill "$p" 2>/dev/null
  for i in {1..50}; do kill -0 "$p" 2>/dev/null || return 0; sleep 0.2; done
  kill -9 "$p" 2>/dev/null
  return 0
}

stop_stack() {
  stop_pid "$SANDBOX_PID"; SANDBOX_PID=""
  stop_pid "$BACKEND_PID"; BACKEND_PID=""
}
trap 'stop_stack' EXIT
trap 'stop_stack; exit 130' INT TERM

require_ports_free() {
  local port
  for port in $BACKEND_PORT $SANDBOX_PORT; do
    if [ -n "$(listeners $port)" ]; then
      echo "abort: something already listens on $port (not started by this script)" >&2
      exit 1
    fi
  done
}

write_config() {  # $1 = home dir, $2 = model
  local m="$2" r
  {
    echo "roles:"
    for r in fast router coding reasoning agent general; do echo "  $r: \"$m\""; done
    for key in tool_capable no_thinking_capable thinking_required_after_tool; do
      echo "$key:"
      for g in $GEMMA_IDS; do echo "  - \"$g\""; done
    done
  } > "$1/config.yaml"
}

run_model() {  # $1 = model id, $2 = result file
  local model="$1" result="$2" i ready
  require_ports_free
  WORK="$(mktemp -d)" || exit 1
  local home="$WORK/wisp-home" sbhome="$WORK/sandbox-home"
  mkdir -p "$home" "$sbhome" || exit 1
  write_config "$home" "$model"
  echo "model: $model"
  # One model in memory at a time (user, 2026-11-12): nothing else stays loaded beside this one.
  "${PY:-python3}" -I "$(dirname "$0")/one_model.py" "$model" || { echo "another model stayed loaded"; exit 6; }
  echo "WISP_HOME: $home"

  ( cd "$REPO" || exit 1
    exec env WISP_HOME="$home" WISP_SANDBOX_HOME="$sbhome" \
      "$PY" -m uvicorn service.main:app --host 127.0.0.1 --port $BACKEND_PORT ) > "$WORK/backend.log" 2>&1 &
  BACKEND_PID=$!

  ready=0
  for i in {1..150}; do
    curl -sf "http://127.0.0.1:$BACKEND_PORT/health" > /dev/null 2>&1 && { ready=1; break; }
    kill -0 "$BACKEND_PID" 2>/dev/null || break
    sleep 0.4
  done
  if [ "$ready" != 1 ]; then
    echo "backend never came up, see $WORK/backend.log" >&2
    mkdir -p "$OUT"; cp "$WORK/backend.log" "$OUT/backend-$model.log" 2>/dev/null
    stop_stack; return 1
  fi

  ( cd "$REPO" || exit 1
    exec env WISP_HOME="$home" WISP_SANDBOX_HOME="$sbhome" \
      WISP_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT" \
      "$PY" -m uvicorn sandbox.server:app --host 127.0.0.1 --port $SANDBOX_PORT ) > "$WORK/sandbox.log" 2>&1 &
  SANDBOX_PID=$!
  ready=0
  for i in {1..75}; do
    curl -sf "http://127.0.0.1:$SANDBOX_PORT/sandbox/status" > /dev/null 2>&1 && { ready=1; break; }
    kill -0 "$SANDBOX_PID" 2>/dev/null || break
    sleep 0.4
  done
  if [ "$ready" != 1 ]; then
    echo "sandbox server never came up, see $WORK/sandbox.log" >&2
    mkdir -p "$OUT"; cp "$WORK/sandbox.log" "$OUT/sandbox-$model.log" 2>/dev/null
    stop_stack; return 1
  fi

  # Let the synthetic Mail/Calendar/Messages/Reminders sync into the backend caches.
  echo "waiting for synthetic sources to sync (up to 60 s)"
  for i in {1..12}; do
    sleep 5
    curl -sf "http://127.0.0.1:$BACKEND_PORT/assistant/sync/status" 2>/dev/null > "$WORK/sync.json"
  done
  cp "$WORK/sync.json" "$OUT/sync-status-$model.json" 2>/dev/null

  local extra=()
  [ "${ANALYZE:-0}" = 1 ] && extra=(--analyze)
  "$PY" "$HERE/demo_flow.py" --model "$model" --base-url "http://127.0.0.1:$BACKEND_PORT" \
    --out "$result" --image "$IMAGE" --wav "$WAV" "${extra[@]}"
  local rc=$?
  cp "$WORK/backend.log" "$OUT/backend-$model.log" 2>/dev/null
  stop_stack
  for i in {1..25}; do
    [ -z "$(listeners $BACKEND_PORT)$(listeners $SANDBOX_PORT)" ] && break
    sleep 0.4
  done
  return $rc
}

mkdir -p "$OUT" || exit 1
echo "results: $OUT"
require_ports_free

CAND_JSON="$OUT/${CAND}.json"
run_model "$CAND" "$CAND_JSON"
FILES=("$CAND_JSON")
if [ -n "$BASE" ] && [ "$BASE" != "$CAND" ]; then
  BASE_JSON="$OUT/${BASE}.json"
  run_model "$BASE" "$BASE_JSON"
  FILES+=("$BASE_JSON")
fi
"$PY" "$HERE/summarize.py" "$OUT" "${FILES[@]}"
