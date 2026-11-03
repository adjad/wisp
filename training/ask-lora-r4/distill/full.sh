#!/bin/bash
# The whole distill run against one build, into a fresh output dir. Every step is resumable, so rerun the same
# command after a stop.
#   full.sh BUILD OUT        e.g. full.sh _build/main raw/main
#     chat_voice_r4 + chat_direct_r4   K=2, top-ups to 10
#     session_chat_r4 finals           K=3, top-ups to 7
#     extra rows (select_extra.py)     K=2, top-ups to 6
#     anchor finals (select_anchor.py) K=2, top-ups to 6           meta.distill_kind "anchor_final"
#     vision rows (../vision)          k=0 greedy as production, top-ups k=1..5 sampled, thinking off
# The first pass draws ONE sample per id (CHAT_K0 / SC_K0 / EXTRA_K0 / ANCHOR_K0, default 1) and the top-ups draw
# more only for ids with no passing sample: the chosen rows are the same as drawing K up front, since choose.py takes
# the first passing sample in k order.
# The heavy lock is held for the WHOLE run: full.sh waits for it to be free, takes it once, snapshots oMLX's
# settings, and on any exit restores them, reloads oMLX and releases the lock. The gen steps (run.sh, HELD_LOCK set)
# only refuse while Wisp is listening. Launch detached:
#   nohup python3 -c "import os; os.setsid(); os.execv('./full.sh', ['./full.sh', BUILD, OUT])" > _build/full.log 2>&1 &
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
PY="${PY:-$HOME/Desktop/MOE_Project/.venv/bin/python}"
LOCK="${LOCK:-/private/tmp/claude-$(id -u)/wisp-heavy.lock}"
VROWS="${VROWS:-$HERE/../vision/vision_rows.jsonl}"
BUILD="$1"; OUT="$2"; P="$BUILD/partial.jsonl"
CHAT_K0="${CHAT_K0:-1}"; SC_K0="${SC_K0:-1}"; EXTRA_K0="${EXTRA_K0:-1}"; ANCHOR_K0="${ANCHOR_K0:-1}"
OMLX_BASE="${OMLX_BASE:-http://127.0.0.1:8000}"
SETTINGS="${OMLX_SETTINGS:-$HOME/.omlx/model_settings.json}"
SNAP="$HERE/_build/model_settings.before.full.$$.json"
ANCHOR_FAMS=right_source,multi_source,grounded_only,mail_r3,web_r3,contacts_r3,device_r3,weather_and_web,concise_gentle,capability_honest,recover_from_tool_error,info_answers_r4,lookup_answers_r4,safe_effects,safe_restraint,ignore_embedded_instructions,no_write_for_reads,restraint_r4
mkdir -p "$OUT" "$HERE/_build"
if lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1; then echo "WISP IS RUNNING - not starting"; exit 4; fi
"$PY" -c "import gen; print('model', gen.MODEL, '->', gen.check_base())" || exit 5
# wait for the lock (another gate or build may hold it), then take it for the whole run; any other mkdir failure aborts
while [ -d "$LOCK" ]; do echo "== $(date) lock held: $(cat "$LOCK/owner" 2>/dev/null)"; sleep 60; done
mkdir "$LOCK" || exit 3
echo "ask-lora r4 distill full.sh pid $$ $(date) (whole run: chat, anchors, vision)" > "$LOCK/owner"
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
export HELD_LOCK="$LOCK"
echo "== $(date) started"
wait_free() {
  while pgrep -f "distill/[a-z_]*gen[.]py" >/dev/null; do sleep 20; done
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
vtopup() {        # vision top-ups: one more sampled answer per round for rows with no passing answer yet
  for ((k = 1; k < 6; k++)); do
    "$PY" vision_choose.py --rows "$VROWS" --raw "$OUT/vision.jsonl" --out-chosen "$OUT/.vision.c.jsonl" \
      --out-stats "$OUT/.vision.s.json" >/dev/null || return 1
    "$PY" -c "import json,sys; print('\n'.join(json.load(open(sys.argv[1]))['overall']['no_pass']))" \
      "$OUT/.vision.s.json" > "$OUT/.vision.ids"
    n=$(grep -c . "$OUT/.vision.ids"); echo "== vision round k=$k: $n rows with no passing answer"
    [ "$n" -eq 0 ] && break
    GEN=vision_gen.py ./run.sh --rows "$VROWS" --ids "$OUT/.vision.ids" --k $((k + 1)) --out "$OUT/vision.jsonl" || return $?
  done
}
# per-build inputs: the session_chat recall facts, the live tool names, the extra and anchor ids
"$PY" refs.py --partial "$P" --out "$OUT/session_chat.refs.json" --registry-out "$OUT/tool_names.json" || exit 1
"$PY" select_extra.py "$P" > "$OUT/extra.ids" || exit 1
"$PY" select_anchor.py "$P" > "$OUT/anchor.ids" || exit 1
export TOOL_NAMES="$OUT/tool_names.json"

step ./run.sh --partial "$P" --families chat_voice_r4,chat_direct_r4 --k "$CHAT_K0" --out "$OUT/chat.jsonl" || exit $?
step ./run.sh --partial "$P" --families session_chat_r4 --k "$SC_K0" --out "$OUT/session_chat.jsonl" || exit $?
step ./run.sh --partial "$P" --families knowledge_direct,concise_gentle --ids "$OUT/extra.ids" --k "$EXTRA_K0" \
  --out "$OUT/extra.jsonl" || exit $?
step ./run.sh --partial "$P" --families "$ANCHOR_FAMS" --ids "$OUT/anchor.ids" --k "$ANCHOR_K0" \
  --out "$OUT/anchor.jsonl" || exit $?
export GEN=vision_gen.py      # an env prefix on a shell function does not reliably reach its commands
step ./run.sh --rows "$VROWS" --k 1 --out "$OUT/vision.jsonl" || exit $?
unset GEN
step ./topup.sh "$P" "$OUT/chat.jsonl" chat_voice_r4,chat_direct_r4 "$CHAT_K0" 10 || exit $?
export REFS="$OUT/session_chat.refs.json"
step ./topup.sh "$P" "$OUT/session_chat.jsonl" session_chat_r4 "$SC_K0" 7 || exit $?
unset REFS
step ./topup.sh "$P" "$OUT/extra.jsonl" knowledge_direct,concise_gentle "$EXTRA_K0" 6 || exit $?
export ANCHORS="$OUT/anchor.ids"
step ./topup.sh "$P" "$OUT/anchor.jsonl" "$ANCHOR_FAMS" "$ANCHOR_K0" 6 || exit $?
unset ANCHORS
wait_free; vtopup || exit $?

"$PY" choose.py --partial "$P" --raw "$OUT/chat.jsonl" --raw "$OUT/session_chat.jsonl" --raw "$OUT/extra.jsonl" \
  --raw "$OUT/anchor.jsonl" --refs "$OUT/session_chat.refs.json" --tool-names "$OUT/tool_names.json" \
  --anchor-ids "$OUT/anchor.ids" --targets "$OUT/extra.ids" \
  --out-chosen "$OUT/chosen.jsonl" --out-stats "$OUT/stats.json"
"$PY" vision_choose.py --rows "$VROWS" --raw "$OUT/vision.jsonl" --out-chosen "$OUT/vision.chosen.jsonl" \
  --out-stats "$OUT/vision.stats.json" --out-rows "$BUILD-vision/vision_rows.jsonl"
echo "== $(date) full done"
