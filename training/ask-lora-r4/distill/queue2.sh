#!/bin/bash
# Second pass (audit chatstyle-1 / thinking-1): knowledge_direct step-0 Q&As and the small-talk rows in
# raw/extra.ids, K=2, then top-up rounds k=2..5. Starts after queue.sh exits. Launch detached like queue.sh:
#   nohup ./queue2.sh > _build/queue2.log 2>&1 &
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
LOCK="${LOCK:-/private/tmp/claude-$(id -u)/wisp-heavy.lock}"
PARTIAL="${PARTIAL:-_build/final/partial.jsonl}"
FAMS=knowledge_direct,concise_gentle
wait_free() {
  while pgrep -f "[.]/queue[.]sh" >/dev/null || pgrep -f "distill/gen.py" >/dev/null || [ -d "$LOCK" ]; do sleep 20; done
}
step() {
  echo "== $(date) $*"
  for try in 1 2 3; do
    wait_free
    "$@"; rc=$?
    [ $rc -ne 3 ] && return $rc
  done
  return 3
}
step ./run.sh --partial "$PARTIAL" --families "$FAMS" --ids raw/extra.ids --k 2 --out raw/extra.jsonl || exit $?
step ./topup.sh "$PARTIAL" raw/extra.jsonl "$FAMS" 2 6 || exit $?
echo "== $(date) queue2 done"
