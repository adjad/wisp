#!/bin/bash
# Last top-up (integrator, 2026-11-02): up to 4 more samples for each id that still has no passing sample
# (chat k=6..9, session_chat k=3..6). Starts after queue2.sh exits. Launch detached like queue.sh.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
LOCK="${LOCK:-/private/tmp/claude-$(id -u)/wisp-heavy.lock}"
PARTIAL="${PARTIAL:-_build/final/partial.jsonl}"
wait_free() {
  while pgrep -f "[.]/queue2?[.]sh" >/dev/null || pgrep -f "distill/gen.py" >/dev/null || [ -d "$LOCK" ]; do sleep 20; done
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
step ./topup.sh "$PARTIAL" raw/chat.jsonl chat_voice_r4,chat_direct_r4 6 10 || exit $?
export REFS=raw/session_chat.refs.json; step ./topup.sh "$PARTIAL" raw/session_chat.jsonl session_chat_r4 3 7 || exit $?
echo "== $(date) queue3 done"
