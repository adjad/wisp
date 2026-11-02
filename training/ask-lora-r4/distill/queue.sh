#!/bin/bash
# The whole r4 distill run, in order, each step resumable (rerunning skips samples already in raw/):
#   1. chat_voice_r4 + chat_direct_r4, K=2
#   2. session_chat_r4 finals, K=3
#   3. top-up rounds k=2..5 for chat ids with no passing sample (topup.sh)
# Waits for any running gen.py and for the heavy lock between steps. Launch detached so a quit app does not kill it:
#   nohup ./queue.sh > _build/queue.log 2>&1 &
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
LOCK="${LOCK:-/private/tmp/claude-$(id -u)/wisp-heavy.lock}"
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
step ./run.sh --partial _build/r4/partial.jsonl --families chat_voice_r4,chat_direct_r4 --k 2 --out raw/chat.jsonl || exit $?
step ./run.sh --partial _build/sc/partial.jsonl --families session_chat_r4 --k 3 --out raw/session_chat.jsonl || exit $?
wait_free
step ./topup.sh _build/r4/partial.jsonl raw/chat.jsonl chat_voice_r4,chat_direct_r4 2 6 || exit $?
echo "== $(date) queue done"
