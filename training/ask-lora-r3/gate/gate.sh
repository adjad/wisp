#!/bin/bash
# Ask LoRA Mac-side gate: model A (the untuned baseline) vs model B (the candidate), same oMLX, one model
# resident at a time. Runs toolbench, the chat eval, the MTP probe, then the vision, video and audio checks.
#
#   gate.sh <modelA> <modelB> [trials=3] [chat_samples=3]
#
# <modelA>/<modelB> are oMLX model ids (folder names), e.g. gemma-4-E4B-it-oQ4e-mtp and the r3 build.
# Needs: Wisp quit, oMLX running on $OMLX_BASE, and the heavy lock free (every Swift compile and every gate takes it).
# Stages are skipped (not faked) when their input is missing; set ONLY=toolbench,chat,mtp,vision,video,audio to run some.
#
# Env (all optional):
#   REPO         the Wisp checkout whose service/ the checks import   (default: this script's repository)
#   PY           python with that repo's deps                         (default: $REPO/../../../.venv or python3)
#   OUT          results dir                                          (default: gate/runs/<A>__<B>__<timestamp>)
#   LOCK         heavy-lock directory                                 (default: /private/tmp/claude-$(id -u)/wisp-heavy.lock)
#   OMLX_BASE    oMLX URL                                             (default: http://127.0.0.1:8000)
#   OMLX_SETTINGS  oMLX per-model settings file                       (default: ~/.omlx/model_settings.json)
#   CHATEVAL     path to chat_eval.py (not in the repo); stage skipped when unset
#   CHAT_PROMPTS chat eval prompts                                    (default: training/ask-lora-r2/chateval_prompts.yaml)
#   VISION_TEST_SET  which images: gate/vision/cases.json is built by make_images.py + make_images_more.py (60 cases)
set -u
A=${1:?usage: gate.sh <modelA> <modelB> [trials] [chat_samples]}
B=${2:?usage: gate.sh <modelA> <modelB> [trials] [chat_samples]}
TRIALS=${3:-3}; SAMPLES=${4:-3}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd "$HERE" && git rev-parse --show-toplevel)}"
if [ -z "${PY:-}" ]; then
  for c in "$REPO/.venv/bin/python" "$HOME/Desktop/MOE_Project/.venv/bin/python"; do [ -x "$c" ] && PY="$c" && break; done
  PY="${PY:-python3}"
fi
LOCK="${LOCK:-/private/tmp/claude-$(id -u)/wisp-heavy.lock}"
OMLX_BASE="${OMLX_BASE:-http://127.0.0.1:8000}"; export OMLX_BASE
SETTINGS="${OMLX_SETTINGS:-$HOME/.omlx/model_settings.json}"
CHAT_PROMPTS="${CHAT_PROMPTS:-$REPO/training/ask-lora-r2/chateval_prompts.yaml}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${OUT:-$HERE/runs/${A}__${B}__$STAMP}"
ONLY="${ONLY:-toolbench,chat,mtp,vision,video,audio}"
want() { case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

# Wisp must be quit: a live Wisp holds the engine and its backend ports.
if lsof -nP -iTCP:8765 -iTCP:8791 -sTCP:LISTEN >/dev/null 2>&1; then echo "WISP IS RUNNING - not starting"; exit 4; fi
mkdir -p "$(dirname "$LOCK")" "$OUT" || exit 2
mkdir "$LOCK" 2>/dev/null || { echo "LOCK HELD: $(cat "$LOCK/owner" 2>/dev/null)"; exit 3; }
echo "ask-lora gate.sh: $A vs $B (trials $TRIALS) pid $$ $(date)" > "$LOCK/owner"

# Everything below is undone by the trap: the lock, the sampler and oMLX's settings.
SAMPLER=""
cp "$SETTINGS" "$OUT/model_settings.before.json" || { echo "cannot snapshot $SETTINGS"; rm -rf "$LOCK"; exit 2; }
restore() {
  cp "$OUT/model_settings.before.json" "$SETTINGS"
  curl -s -m60 -X POST "$OMLX_BASE/admin/api/reload" >/dev/null
  echo "== $(date) oMLX settings restored"
  [ -n "$SAMPLER" ] && kill "$SAMPLER" 2>/dev/null
  rm -rf "$LOCK"
}
trap restore EXIT

# memory sampler: omlx RSS (KB), swap used, pressure, every 5 s
( while :; do echo "$(date +%s),$(ps -axo rss,comm | awk '/omlx/ {s+=$1} END {print s}'),$(sysctl -n vm.swapusage | awk '{print $6}'),$(memory_pressure -Q 2>/dev/null | awk '/percentage/ {print $5}')"; sleep 5; done ) > "$OUT/memory.csv" &
SAMPLER=$!

# A model without max_context_window is budgeted 8k by Wisp against 16k for one that declares it. Give B the
# same window as A for the run so the comparison is not about the budget (restored on exit).
"$PY" - "$SETTINGS" "$A" "$B" <<'PY'
import json, pathlib, sys
p, a, b = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
d = json.loads(p.read_text()); ms = d.get("models", d)
want = (ms.get(a) or {}).get("max_context_window")
if want and b in ms and not ms[b].get("max_context_window"):
    ms[b]["max_context_window"] = want; p.write_text(json.dumps(d, indent=2)); print(f"max_context_window {want} copied A -> B for this run")
PY
curl -s -m60 -X POST "$OMLX_BASE/admin/api/reload"; echo

if want toolbench; then
  echo "== $(date) TOOLBENCH start"
  (cd "$REPO" && nice -n 5 "$PY" scripts/run_toolbench.py --models "$A,$B" --exclusive --trials "$TRIALS")
  echo "== $(date) TOOLBENCH EXIT $?"
fi
if want chat; then
  if [ -n "${CHATEVAL:-}" ] && [ -f "$CHATEVAL" ]; then
    (cd "$(dirname "$CHATEVAL")" && nice -n 5 "$PY" "$CHATEVAL" --repo "$REPO" --prompts "$CHAT_PROMPTS" \
       --models "$A,$B" --profile-as "$A" --samples "$SAMPLES" --out "$OUT/chateval")
    echo "== $(date) CHATEVAL EXIT $?"
  else echo "== CHATEVAL skipped (set CHATEVAL=/path/to/chat_eval.py)"; fi
fi
if want mtp; then
  nice -n 5 "$PY" "$HERE/mtp_probe.py" --models "$A,$B" --out "$OUT/mtp_probe.json"
  echo "== $(date) MTP EXIT $?"
fi
if want vision; then
  cd "$HERE/vision" || exit 2
  [ -f cases.json ] || { echo "== vision images missing: building (make_images_more.py runs make_images.py first)"; "$PY" make_images_more.py || exit 2; }
  grep -q '"scanned_document"' cases.json || { echo "== scanned-document cases missing: building"; "$PY" make_documents.py || exit 2; }
  nice -n 5 "$PY" run_vision.py run --repo "$REPO" --models "$A,$B" --out "$OUT/vision"
  echo "== $(date) VISION EXIT $?"
  "$PY" run_vision.py score "$OUT/vision/answers.jsonl" --a "$A" --b "$B" --repo "$REPO" --json-out "$OUT/vision/score.json" \
    > "$OUT/vision/score.txt" 2>&1; echo "== vision scored -> $OUT/vision/score.txt"
fi
if want video; then
  cd "$HERE/vision" || exit 2
  [ -f video/clips.json ] || "$PY" run_video.py make
  nice -n 5 "$PY" run_video.py run --repo "$REPO" --models "$A,$B" --out "$OUT/vision"
  echo "== $(date) VIDEO EXIT $?"
fi
if want audio; then
  cd "$HERE/audio" || exit 2
  [ -f truth.json ] || "$PY" make_audio.py
  nice -n 5 "$PY" run_audio.py run --repo "$REPO" --models "$A,$B" --out "$OUT/audio"
  echo "== $(date) AUDIO EXIT $?"
  "$PY" run_audio.py score "$OUT/audio/answers.jsonl" --json-out "$OUT/audio/score.json" > "$OUT/audio/score.txt" 2>&1
  echo "== audio scored -> $OUT/audio/score.txt"
fi
if want vision && want audio; then
  "$PY" "$HERE/mm_gate.py" "$OUT/vision/score.json" "$OUT/audio/score.json" --a "$A" --b "$B" | tee "$OUT/multimodal-gate.txt"
fi
echo "== done; results in $OUT"
