#!/bin/zsh
# One real Gemma-Wisp download through Wisp's setup path, ISOLATED: throwaway WISP_HOME,
# backend on 8775, synthetic-sources sandbox on 8766, the built-in engine on 8791 taken from
# the installed app (read-only). Never uses 8765, never touches oMLX on :8000 (aborts if a
# model is loaded there, so only one model is ever resident). Kills only the PIDs it started.
#   REPO=<checkout with the roster code> OUT=<dir> zsh hub_download.sh

HERE="${0:A:h}"
REPO="${REPO:-/Users/adijain/Desktop/MOE_Project/.claude/worktrees/r5-window-test}"
PY="${PY:-/Users/adijain/Desktop/MOE_Project/.venv/bin/python}"
ENGINE_ROOT="${ENGINE_ROOT:-/Applications/Wisp.app/Contents/Resources/engine}"
MANIFEST="${MANIFEST:-$HERE/../hf/MANIFEST.sha256-size.txt}"
OUT="${OUT:?set OUT to a results folder}"
mkdir -p "$OUT" || exit 1
WORK="$OUT/work"; home="$WORK/wisp-home"; sbhome="$WORK/sandbox-home"
mkdir -p "$home" "$sbhome" || exit 1
BACKEND_PID=""; SANDBOX_PID=""

listeners() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null; }
stop_pid() { local p="$1" i; [ -n "$p" ] || return 0; kill -0 "$p" 2>/dev/null || return 0
  kill "$p" 2>/dev/null; for i in {1..75}; do kill -0 "$p" 2>/dev/null || return 0; sleep 0.2; done; kill -9 "$p" 2>/dev/null; }
stop_all() {
  stop_pid "$SANDBOX_PID"; SANDBOX_PID=""
  stop_pid "$BACKEND_PID"; BACKEND_PID=""
  # The engine child is ours only if its pid record lives in our throwaway home.
  local rec p
  for rec in "$home"/engine/**/engine.pid(N); do
    p="$(awk '{print $1; exit}' "$rec")"
    [ -n "$p" ] && [ "$(listeners 8791)" = "$p" ] && stop_pid "$p"
  done
  [ -n "$(listeners 8791)" ] && echo "WARN: something still listens on 8791 (pid $(listeners 8791))"
}
trap 'stop_all' EXIT
trap 'stop_all; exit 130' INT TERM

for port in 8775 8766 8791; do
  [ -z "$(listeners $port)" ] || { echo "abort: $port is in use"; exit 1; }
done
loaded="$(curl -s -m5 http://127.0.0.1:8000/v1/models/status | "$PY" -c "import json,sys;print(json.load(sys.stdin)['loaded_count'])" 2>/dev/null)"
[ "${loaded:-0}" = 0 ] || { echo "abort: $loaded model(s) loaded on :8000; one model resident at a time"; exit 6; }
[ -f "$ENGINE_ROOT/ENGINE.json" ] || { echo "abort: no built-in engine at $ENGINE_ROOT"; exit 1; }
echo "repo $(git -C "$REPO" log --oneline -1)"
echo "WISP_HOME $home"

( cd "$REPO" || exit 1
  exec env WISP_HOME="$home" WISP_SANDBOX_HOME="$sbhome" WISP_ENGINE_MODE=bundled \
    WISP_ENGINE_ROOT="$ENGINE_ROOT" \
    "$PY" -m uvicorn service.main:app --host 127.0.0.1 --port 8775 ) > "$OUT/backend.log" 2>&1 &
BACKEND_PID=$!
for i in {1..150}; do curl -sf http://127.0.0.1:8775/health >/dev/null 2>&1 && break
  kill -0 "$BACKEND_PID" 2>/dev/null || { echo "backend died, see $OUT/backend.log"; exit 1; }; sleep 0.4; done
( cd "$REPO" || exit 1
  exec env WISP_HOME="$home" WISP_SANDBOX_HOME="$sbhome" WISP_BACKEND_URL=http://127.0.0.1:8775 \
    "$PY" -m uvicorn sandbox.server:app --host 127.0.0.1 --port 8766 ) > "$OUT/sandbox.log" 2>&1 &
SANDBOX_PID=$!
for i in {1..75}; do curl -sf http://127.0.0.1:8766/sandbox/status >/dev/null 2>&1 && break; sleep 0.4; done

"$PY" -I "$HERE/hub_download.py" --home "$home" --manifest "$MANIFEST" --out "$OUT/report.json"
rc=$?
echo "loaded on :8000 after: $(curl -s -m5 http://127.0.0.1:8000/v1/models/status | "$PY" -c "import json,sys;print(json.load(sys.stdin)['loaded_count'])" 2>/dev/null)"
stop_all
echo "EXIT $rc"
exit $rc
