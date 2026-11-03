#!/bin/bash
# The whole distill run against one build, into a fresh output dir. Every step is resumable, so rerun the same
# command after a stop.
#   full.sh BUILD OUT        e.g. full.sh _build/main raw/main
#     chat_voice_r4 + chat_direct_r4   K=2, top-ups to 10
#     session_chat_r4 finals           K=3, top-ups to 7
#     extra rows (select_extra.py)     K=2, top-ups to 6
# Each gen step goes through run.sh (heavy lock, oMLX settings restored on exit). Launch detached:
#   nohup python3 -c "import os; os.setsid(); os.execv('./full.sh', ['./full.sh', BUILD, OUT])" > _build/full.log 2>&1 &
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
PY="${PY:-$HOME/Desktop/MOE_Project/.venv/bin/python}"
LOCK="${LOCK:-/private/tmp/claude-$(id -u)/wisp-heavy.lock}"
BUILD="$1"; OUT="$2"; P="$BUILD/partial.jsonl"
mkdir -p "$OUT"
wait_free() {
  while pgrep -f "distill/gen.py" >/dev/null || [ -d "$LOCK" ]; do sleep 20; done
}
step() {
  echo "== $(date) $*"
  for try in 1 2 3; do
    wait_free
    "$@"; rc=$?
    [ $rc -ne 3 ] && return $rc       # 3 = lock taken between the check and mkdir: wait and retry
  done
  return 3
}
# per-build inputs: the session_chat recall facts, the live tool names, the extra ids
"$PY" refs.py --partial "$P" --out "$OUT/session_chat.refs.json" --registry-out "$OUT/tool_names.json" || exit 1
"$PY" select_extra.py "$P" > "$OUT/extra.ids" || exit 1
export TOOL_NAMES="$OUT/tool_names.json"

step ./run.sh --partial "$P" --families chat_voice_r4,chat_direct_r4 --k 2 --out "$OUT/chat.jsonl" || exit $?
step ./run.sh --partial "$P" --families session_chat_r4 --k 3 --out "$OUT/session_chat.jsonl" || exit $?
step ./run.sh --partial "$P" --families knowledge_direct,concise_gentle --ids "$OUT/extra.ids" --k 2 \
  --out "$OUT/extra.jsonl" || exit $?
step ./topup.sh "$P" "$OUT/chat.jsonl" chat_voice_r4,chat_direct_r4 2 10 || exit $?
export REFS="$OUT/session_chat.refs.json"
step ./topup.sh "$P" "$OUT/session_chat.jsonl" session_chat_r4 3 7 || exit $?
unset REFS
step ./topup.sh "$P" "$OUT/extra.jsonl" knowledge_direct,concise_gentle 2 6 || exit $?

"$PY" choose.py --partial "$P" --raw "$OUT/chat.jsonl" --raw "$OUT/session_chat.jsonl" --raw "$OUT/extra.jsonl" \
  --refs "$OUT/session_chat.refs.json" --tool-names "$OUT/tool_names.json" \
  --out-chosen "$OUT/chosen.jsonl" --out-stats "$OUT/stats.json"
echo "== $(date) full done"
