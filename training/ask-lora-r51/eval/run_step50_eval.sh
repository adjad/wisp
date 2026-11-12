#!/bin/zsh
# r5.1 step-50 gate: base, r5 step 1800 and (when given) the r5.1 step-50 candidate on the SAME frozen cases, world,
# clock, seeds and decoding, in two toolbench runs: every case once (seed 1001), then the safety slice 5 times
# (seeds 2001-2005). Then score_step50.py decides CONTINUE / BLOCK / INCOMPLETE (CRITERIA.md).
#
#   CAND=<oMLX id of the merged+quantized r5.1 step-50 checkpoint> zsh run_step50_eval.sh
#   zsh run_step50_eval.sh            # no CAND: a baseline dry run of base and r5 step 1800 only
#
# Quits Wisp (its quit runs `omlx-cli stop`, so oMLX is started again here), takes the heavy lock first, gives every
# candidate exactly base's oMLX entry, and reopens Wisp at the end. Tool effects are toolbench's synthetic stubs.
setopt pipefail
A=gemma-4-E4B-it-oQ4e-mtp
REF=gemma-4-E4B-it-r5-step-01800-oQ4e-mtp
MODELS="$A,$REF${CAND:+,$CAND}"
REPO=$HOME/Desktop/r5-pilot/wisp-main-7699a96b
PY=/Users/adijain/Desktop/MOE_Project/.venv/bin/python
HERE=${0:A:h}
WRAP=$HERE/../same-clock/clock_wrapper.py
export PRIVATE_EVAL_DIR=$HOME/.moe/evals/r51-step50
RUN=${RUN:-$HOME/Desktop/r5-pilot/step50-gate-$(date +%Y%m%d-%H%M%S)}
LOCK=/private/tmp/claude-$(id -u)/wisp-heavy.lock
mkdir -p "$RUN" || exit 2
say() { echo "== $(date -u +%T) $*"; }
[ "$(git -C "$REPO" rev-parse --short HEAD)" = 8618a299 ] || { say "test checkout moved"; exit 2; }
(cd "$PRIVATE_EVAL_DIR" && shasum -a 256 -c "$HERE/CASES.sha256") || { say "frozen cases changed: INCOMPLETE"; exit 3; }
SAFETY=$("$PY" -I -c "import json;print(','.join(json.load(open('$HERE/SLICES.json'))['safety']))") || exit 2

say "waiting for the heavy lock"
until mkdir "$LOCK" 2>/dev/null; do sleep 30; done
echo "r5.1 step-50 gate pid $$ $(date)" > "$LOCK/owner" || { rmdir "$LOCK"; exit 2; }
trap 'rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null' EXIT
say "quitting Wisp"
osascript -e 'quit app "Wisp"' 2>&1
trap 'rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null; say "reopening Wisp"; open -a Wisp' EXIT
for i in $(seq 1 60); do lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 2; done
lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1 && { say "Wisp still listening; stopping"; exit 4; }
sleep 8
/Applications/oMLX.app/Contents/MacOS/omlx-cli start --no-wait; say "omlx-cli start exit $?"
for i in $(seq 1 60); do [ "$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/v1/models)" = 200 ] && break; sleep 5; done
cp ~/.omlx/model_settings.json "$RUN/model_settings.before.json" || exit 2
"$PY" -I - "$A" "${(s:,:)MODELS}" <<'PYE' || exit 2
import json, pathlib, sys, copy
p = pathlib.Path.home() / ".omlx/model_settings.json"
a, others = sys.argv[1], [m for m in sys.argv[2:] if m != sys.argv[1]]
d = json.loads(p.read_text()); ms = d.get("models", d)
for b in others:
    ms[b] = copy.deepcopy(ms[a]); print("set", b, "= exactly", a)
p.write_text(json.dumps(d, indent=2))
PYE
curl -s -m60 -X POST http://127.0.0.1:8000/admin/api/reload; echo
for M in ${(s:,:)MODELS}; do
  for i in $(seq 1 30); do curl -s -m10 http://127.0.0.1:8000/v1/models | grep -q "\"$M\"" && break; sleep 10; done
  curl -s -m10 http://127.0.0.1:8000/v1/models | grep -q "\"$M\"" || { say "$M not listed: INCOMPLETE"; exit 3; }
done
say "MAIN: every case, 1 trial, $MODELS"
(cd "$REPO" && nice -n 5 "$PY" "$WRAP" --private --models "$MODELS" --profile-as "$A" --exclusive --trials 1 \
   --seed-base 1000 --out "$RUN/main") > "$RUN/main.log" 2>&1; say "MAIN EXIT $?"
say "SAFETY: safety slice, 5 trials"
(cd "$REPO" && nice -n 5 "$PY" "$WRAP" --private --models "$MODELS" --profile-as "$A" --exclusive --trials 5 \
   --seed-base 2000 --ids "$SAFETY" --out "$RUN/safety") > "$RUN/safety.log" 2>&1; say "SAFETY EXIT $?"
(cd "$REPO" && "$PY" "$HERE/score_step50.py" --main "$RUN/main" --safety "$RUN/safety" --base "$A" --ref "$REF" \
   ${CAND:+--cand} ${CAND:+"$CAND"} --slices "$HERE/SLICES.json" --hashes "$HERE/CASES.sha256" --out "$RUN/gate"); rc=$?
say "GATE EXIT $rc (0 CONTINUE or BASELINE, 2 BLOCK, 3 INCOMPLETE)  $RUN/gate/GATE.md"
exit $rc
