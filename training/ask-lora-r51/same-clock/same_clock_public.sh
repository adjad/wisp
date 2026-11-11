#!/bin/zsh
# Same-clock public toolbench: base and r5 step 1800 in ONE run, one explicit simulated timestamp.
# Same harness commit (test checkout 8618a299 = main 7699a96b + #499 + r5 branch), cases, seeds (1000+t), decoding
# (each candidate gets exactly base's oMLX entry, --profile-as base, --exclusive), per-case session reset (#499).
# Tool effects are the harness's synthetic stubs. Quits Wisp (its quit stops oMLX, so oMLX is started again here).
setopt pipefail
A=gemma-4-E4B-it-oQ4e-mtp
B=gemma-4-E4B-it-r5-step-01800-oQ4e-mtp
WHEN=${WHEN:-2026-11-11}            # world "now" = WHEN at the world's 08:30
TRIALS=${TRIALS:-1}
REPO=$HOME/Desktop/r5-pilot/wisp-main-7699a96b
PY=/Users/adijain/Desktop/MOE_Project/.venv/bin/python
HERE=${0:A:h}
RUN=$HOME/Desktop/r5-pilot/same-clock-$(date +%Y%m%d-%H%M%S)
LOCK=/private/tmp/claude-$(id -u)/wisp-heavy.lock
mkdir -p "$RUN" || exit 2
say() { echo "== $(date -u +%T) $*"; }
[ "$(git -C "$REPO" rev-parse --short HEAD)" = 8618a299 ] || { say "test checkout moved"; exit 2; }
[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ] || { say "test checkout dirty"; exit 2; }

say "waiting for the heavy lock"
until mkdir "$LOCK" 2>/dev/null; do sleep 30; done
echo "r5.1 same-clock public toolbench pid $$ $(date)" > "$LOCK/owner" || { rmdir "$LOCK"; exit 2; }
trap 'rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null' EXIT
say "lock taken"
say "quitting Wisp"
osascript -e 'quit app "Wisp"' 2>&1
trap 'rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null; say "reopening Wisp"; open -a Wisp' EXIT
for i in $(seq 1 60); do lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 2; done
lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1 && { say "Wisp still listening; stopping"; exit 4; }
sleep 8
/Applications/oMLX.app/Contents/MacOS/omlx-cli start --no-wait; say "omlx-cli start exit $?"
for i in $(seq 1 60); do [ "$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/v1/models)" = 200 ] && break; sleep 5; done
cp ~/.omlx/model_settings.json "$RUN/model_settings.before.json" || exit 2
"$PY" -I - "$A" "$B" <<'PYE' || exit 2
import json, pathlib, sys, copy
p = pathlib.Path.home() / ".omlx/model_settings.json"
a, b = sys.argv[1], sys.argv[2]
d = json.loads(p.read_text()); ms = d.get("models", d)
ms[b] = copy.deepcopy(ms[a]); p.write_text(json.dumps(d, indent=2)); print("set", b, "= exactly", a)
PYE
curl -s -m60 -X POST http://127.0.0.1:8000/admin/api/reload; echo
for i in $(seq 1 30); do curl -s -m10 http://127.0.0.1:8000/v1/models | grep -q "\"$B\"" && break; sleep 10; done
curl -s -m10 http://127.0.0.1:8000/v1/models | grep -q "\"$B\"" || { say "$B not listed"; exit 5; }

say "TOOLBENCH same-clock start: $A,$B world-date $WHEN trials $TRIALS"
(cd "$REPO" && nice -n 5 "$PY" "$HERE/clock_wrapper.py" --models "$A,$B" --profile-as "$A" --exclusive \
   --trials "$TRIALS" --world-date "$WHEN" --out "$RUN/toolbench") > "$RUN/toolbench.log" 2>&1
say "TOOLBENCH EXIT $?"
say "ALL DONE $RUN"
