#!/bin/zsh
# Base generation for safety_contrast_r51 on the Mac's oMLX, queued behind the step-50 baseline.
# Holds the heavy lock from before Wisp quits until Wisp reopens; run.sh gets a nested lock inside it.
#   D=<pipeline OUT> (default data/pipe)   IDS=<file of meta ids> (optional: only those rows, a top-up after a rebuild)
setopt pipefail
W=/Users/adijain/Desktop/MOE_Project/.claude/worktrees/ask-lora-brev-2caec5
D=${D:-$W/training/ask-lora-r51/data/pipe}
IDARG=(); [ -n "${IDS:-}" ] && IDARG=(--ids "$IDS")
LOCK=/private/tmp/claude-$(id -u)/wisp-heavy.lock
say() { echo "== $(date -u +%T) $*"; }
say "waiting for the step-50 baseline to finish"
while pgrep -f run_step50_eval.sh >/dev/null; do sleep 60; done
say "waiting for the heavy lock"
until mkdir "$LOCK" 2>/dev/null; do sleep 30; done
echo "r5.1 safety base gen pid $$ $(date)" > "$LOCK/owner" || { rmdir "$LOCK"; exit 2; }
trap 'rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null' EXIT
say "quitting Wisp"
osascript -e 'quit app "Wisp"' 2>&1
trap 'rm -rf "$LOCK/gen"; rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null; say "reopening Wisp"; open -a Wisp' EXIT
for i in $(seq 1 60); do lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 2; done
lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1 && { say "Wisp still listening; stopping"; exit 4; }
sleep 8
/Applications/oMLX.app/Contents/MacOS/omlx-cli start --no-wait; say "omlx-cli start exit $?"
for i in $(seq 1 60); do [ "$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/v1/models)" = 200 ] && break; sleep 5; done
say "oMLX models http $(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/v1/models)"
mkdir -p "$D/raw" || exit 2
say "GEN start"
LOCK="$LOCK/gen" bash $W/training/ask-lora-r4/distill/run.sh --partial $D/sealed/partial.jsonl --families safety_contrast_r51 --k 2 --out $D/raw/base.jsonl "${IDARG[@]}"
say "GEN EXIT $?"
