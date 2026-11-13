#!/bin/zsh
# 0.5.0 Mac checks on the final quantized r5 step 1800 vs base Gemma, in ONE window with Wisp quit.
# Run only after the user's OK (relayed by the coordinator): it quits Wisp, which also stops oMLX's server,
# loads both models in turn, and reopens Wisp at the end.
#
#   1. rebuild the private toolbench from ~/Downloads/wisp-debug-*.json (standing rule)
#   2. background jobs, weighted like Ask (coordinator, 2026-11-12): background_probe.py (digests, message topics,
#      Brief messages, memory capture, search) and proactive_probe.py (triage, judge, auto-add, held time,
#      follow-ups, reply drafts), base and step 1800 on the same synthetic inputs
#   3. the Mac multimodal gate: MTP probe, vision, picture reading, picture turns, audio (never run on step 1800)
#   4. the demo flow on an isolated backend (8775 + sandbox 8766): Ask with tools, Brief, Day, look at screen, voice
#   5. private toolbench, base and step 1800 in ONE run (the 84 vs 72 number was two runs), last because longest
#
# Code under test: r5-window-test, a LOCAL checkout: the 0.5.0 roster branch (origin/main e27dcd1b + 8a0a54ab)
# merged with the r5 branch for the private toolbench and #499, never pushed. The step-1800 entry in
# oMLX's per-model settings is set to exactly base's for the run, and the file is restored afterwards.
setopt pipefail
A=gemma-4-E4B-it-oQ4e-mtp
B=gemma-4-E4B-it-r5-step-01800-oQ4e-mtp
REPO=/Users/adijain/Desktop/MOE_Project/.claude/worktrees/r5-window-test
T=/Users/adijain/Desktop/MOE_Project/.claude/worktrees/ask-lora-brev-2caec5/training
PY=/Users/adijain/Desktop/MOE_Project/.venv/bin/python
PRIV=$HOME/.moe/evals/private-real-use/debug-logs
RUN=$HOME/Desktop/r5-pilot/window-0.5.0-$(date +%Y%m%d-%H%M%S)
LOCK=/private/tmp/claude-$(id -u)/wisp-heavy.lock
SETTINGS=$HOME/.omlx/model_settings.json
mkdir -p "$RUN" || exit 2
say() { echo "== $(date -u +%T) $*"; }
cd "$REPO" || exit 2
[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ] || { say "test checkout dirty"; exit 2; }
say "checkout $(git -C "$REPO" rev-parse --short HEAD)"

say "1. rebuilding the private set from ~/Downloads debug logs"
"$PY" -I "$PRIV/tools/build.py" > "$RUN/private-build.log" 2>&1 || { say "private build failed"; tail -20 "$RUN/private-build.log"; exit 2; }
tail -3 "$RUN/private-build.log"

say "waiting for the heavy lock"
until mkdir "$LOCK" 2>/dev/null; do sleep 30; done
echo "0.5.0 r5 window checks pid $$ $(date)" > "$LOCK/owner" || { rmdir "$LOCK"; exit 2; }
restore() {
  [ -f "$RUN/model_settings.before.json" ] && cp "$RUN/model_settings.before.json" "$SETTINGS" && \
    curl -s -m60 -X POST http://127.0.0.1:8000/admin/api/reload >/dev/null; say "oMLX settings restored"
  rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null
  say "reopening Wisp"; open -a Wisp
}
say "quitting Wisp"
osascript -e 'quit app "Wisp"' 2>&1
trap restore EXIT
for i in $(seq 1 60); do lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 2; done
lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1 && { say "Wisp still listening; stopping"; exit 4; }
sleep 8
/Applications/oMLX.app/Contents/MacOS/omlx-cli start --no-wait; say "omlx-cli start exit $?"
for i in $(seq 1 60); do [ "$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/v1/models)" = 200 ] && break; sleep 5; done
cp "$SETTINGS" "$RUN/model_settings.before.json" || exit 2
"$PY" -I - "$A" "$B" "$SETTINGS" <<'PYE' || exit 2
import json, pathlib, sys, copy
a, b, path = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
d = json.loads(path.read_text()); ms = d.get("models", d)
ms[b] = copy.deepcopy(ms[a]); path.write_text(json.dumps(d, indent=2)); print("set", b, "= exactly", a)
PYE
curl -s -m60 -X POST http://127.0.0.1:8000/admin/api/reload; echo
for i in $(seq 1 30); do curl -s -m10 http://127.0.0.1:8000/v1/models | grep -q "\"$B\"" && break; sleep 10; done
curl -s -m10 http://127.0.0.1:8000/v1/models | grep -q "\"$B\"" || { say "$B not listed"; exit 5; }

say "2. background jobs: base and step 1800 on the same synthetic inputs"
for M in "$A" "$B"; do
  WH=$(mktemp -d "$RUN/wisphome.XXXX") || exit 2
  (cd "$REPO" && WISP_HOME="$WH" "$PY" training/ask-lora-r4/background_probe.py --model "$M" --reps 3 --out "$RUN/background-$M.json") \
    > "$RUN/background-$M.log" 2>&1
  say "background probe $M EXIT $?"
done
if [ -f "$T/ask-lora-r5/demo/proactive_probe.py" ]; then
  WH=$(mktemp -d "$RUN/wisphome.XXXX") || exit 2
  (cd "$REPO" && WISP_HOME="$WH" "$PY" "$T/ask-lora-r5/demo/proactive_probe.py" --repo . --models "$A,$B" --reps 3 --out "$RUN/proactive.json") \
    > "$RUN/proactive.log" 2>&1
  say "proactive probe EXIT $?"
else
  say "proactive_probe.py missing: skipped"
fi

say "3. multimodal gate"
rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null   # gate.sh takes the heavy lock itself
REPO="$REPO" PY="$PY" OUT="$RUN/gate" ONLY=mtp,vision,reading,pturns,audio \
  bash "$T/ask-lora-r3/gate/gate.sh" "$A" "$B" 1 1 > "$RUN/gate.log" 2>&1
say "GATE EXIT $?"
until mkdir "$LOCK" 2>/dev/null; do sleep 30; done
echo "0.5.0 r5 window checks pid $$ $(date)" > "$LOCK/owner"

say "4. demo flow (isolated backend 8775, sandbox 8766)"
REPO="$REPO" OUT="$RUN/demo" CAND="$B" BASE="$A" zsh "$T/ask-lora-r5/demo/demo_flow.sh" > "$RUN/demo.log" 2>&1
say "DEMO EXIT $?"

say "5. private toolbench, both models in one run (one pinned clock): $A,$B"
(cd "$REPO" && PRIVATE_EVAL_DIR=$PRIV nice -n 5 "$PY" "$T/ask-lora-r51/same-clock/clock_wrapper.py" --private \
   --models "$A,$B" --profile-as "$A" --exclusive --trials 1 --out "$RUN/toolbench-private") \
   > "$RUN/toolbench-private.log" 2>&1
say "PRIVATE EXIT $?"
(cd "$REPO" && PRIVATE_EVAL_DIR=$PRIV "$PY" scripts/run_toolbench.py --report "$RUN/toolbench-private") > "$RUN/private-report.txt" 2>&1
say "ALL DONE $RUN"
