#!/bin/bash
# One new builder family -> finished r5.1 training rows, reusing r5's tools. Steps that need no model run here;
# base generation is a STOP POINT (it needs oMLX and the heavy lock) and is never started by this script.
#
#   pipeline_r51.sh FAMILY[,FAMILY...] OUT                 # build, seal twice, print the gen command, stop
#   GEN=done pipeline_r51.sh FAMILY OUT                    # resume after OUT/raw/*.jsonl exists: choose, finish, validate
#
# FAMILY is a builder family name (e.g. safety_contrast_r51; write_contrast_r5 as a stand-in). OUT must be new for the first
# run; the resume reuses it. Reading ~/.moe/evals needs the sandbox off. Manifests hold ids only: no eval wording is copied.
#
# Env: PY, PIN (service pin, default r5's pin-7e442f2d), PRIVATE (r5's private debug-log set), STEP50 (sealed r5.1 eval set),
#      AUDIT (the audit outputs r5 sealed against), GEN_SH (default ask-lora-r4/distill/run.sh), K (default 2),
#      ALLOW_INCOMPLETE=1 (pipeline check only: finish tolerates finals base never answered; the output can't be packed).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)" || exit 1
T="$(cd "$HERE/../.." && pwd)" || exit 1
FAMILY="${1:?FAMILY (comma-separated ok)}"; D="${2:?OUT}"
PY="${PY:-$HOME/Desktop/MOE_Project/.venv/bin/python}"
PIN="${PIN:-$T/ask-lora-r5/data/pin-7e442f2d}"
PRIVATE="${PRIVATE:-$HOME/.moe/evals/private-real-use/debug-logs}"
STEP50="${STEP50:-$HOME/.moe/evals/r51-step50}"
AUDIT="${AUDIT:-$HOME/Documents/Codex/2026-11-07/referenced-chatgpt-conversation-this-is-an/outputs}"
GEN_SH="${GEN_SH:-$T/ask-lora-r4/distill/run.sh}"
K="${K:-2}"
die() { echo "FAIL: $1"; exit 1; }
rows_in() { wc -l < "$1/meta.jsonl" | tr -d ' '; }

mkdir -p "$D" || exit 1
D="$(cd "$D" && pwd)" || exit 1

if [ "${GEN:-}" != "done" ]; then
  [ -e "$D/build" ] && die "$D/build exists (use GEN=done to resume, or a new OUT)"
  # (a) build only this family with the real builder (r5's env + ASK_LORA_R51=1)
  IFS=, read -ra FAMS <<< "$FAMILY"
  WISP_REPO="$PIN" "$PY" "$HERE/build_family.py" --out "$D/build" "${FAMS[@]}" > "$D/build.log" 2>&1 || { tail -5 "$D/build.log"; die "build: see $D/build.log"; }
  grep -v "Deprecat\|noqa\|^  from" "$D/build.log" | cut -c1-200
  echo "built: $(rows_in "$D/build") rows"
  cp -R "$D/build" "$D/sealed" || exit 1

  # (b) seal 1 = what r5 sealed against: public toolbench with history + chat eval, the private debug-log set, the audit prompts.
  #     seal 2 = the sealed r5.1 eval set. Each keeps its own manifest (ids and scores only).
  mkdir -p "$D/seal" || exit 1
  cd "$T/ask-lora-r5/mi355x" || exit 1
  env -u WISP_REPO "$PY" seal_r5.py --build "$D/sealed" --private-dir "$PRIVATE" --audit-dir "$AUDIT" > "$D/seal/seal1.log" 2>&1 || die "seal 1: see $D/seal/seal1.log"
  cp "$D/sealed/overlap_manifest.json" "$D/seal/overlap_manifest.r5-sets.json" || exit 1
  echo "seal 1 (public+chat eval, $PRIVATE, audit): $(tail -1 "$D/seal/seal1.log" | cut -c1-400)"
  env -u WISP_REPO "$PY" seal_r5.py --build "$D/sealed" --private-dir "$STEP50" > "$D/seal/seal2.log" 2>&1 || die "seal 2: see $D/seal/seal2.log"
  cp "$D/sealed/overlap_manifest.json" "$D/seal/overlap_manifest.r51-step50.json" || exit 1
  echo "seal 2 ($STEP50): $(tail -1 "$D/seal/seal2.log" | cut -c1-400)"
  # every removed row id, from both seals, for the merge step
  "$PY" - "$D" <<'PYEOF' || exit 1
import json, sys
d = sys.argv[1]
ids = {}
for name in ("r5-sets", "r51-step50"):
    m = json.load(open(f"{d}/seal/overlap_manifest.{name}.json"))
    for t in m["removed"]:
        ids.setdefault(t["turn"], name)
json.dump(ids, open(f"{d}/seal/removed_turns.json", "w"), indent=1, sort_keys=True)
before = sum(1 for _ in open(f"{d}/build/meta.jsonl")); after = sum(1 for _ in open(f"{d}/sealed/meta.jsonl"))
print(f"sealed: {before} -> {after} rows ({len(ids)} turns removed in all); turn ids in {d}/seal/removed_turns.json")
PYEOF
  [ "$(rows_in "$D/sealed")" -gt 0 ] || die "the seals removed every row"
  "$PY" "$T/ask-lora-r5/mi355x/verify_contract.py" --build "$D/sealed" --out "$D/sealed/contract_verification.json" > "$D/seal/verify_contract.log" 2>&1
  if [ $? -eq 0 ]; then echo "contract verification ok ($D/sealed/contract_verification.json)"
  else echo "WARN: verify_contract.py failed (it self-tests r5's gold kinds and may not know this family's gold); not fatal here, see $D/seal/verify_contract.log"; fi
fi

# (c) STOP POINT: base's own answers. One run samples every row of the family (finals AND tool-call steps), k samples each;
#     choose_r5 reads the final rows, choose_traces the call rows, both from the same raw files.
mkdir -p "$D/raw"
GEN_CMD="bash $GEN_SH --partial $D/sealed/partial.jsonl --families $FAMILY --k $K --out $D/raw/base.jsonl"
if [ "${GEN:-}" != "done" ] || ! ls "$D"/raw/*.jsonl >/dev/null 2>&1; then
  echo
  echo "STOP: base's answers are needed, and that needs oMLX on :8000, Wisp quit, and the heavy lock (run.sh takes it)."
  echo "Run this on the Mac when oMLX is free (resumable; rerun skips finished samples):"
  echo "  $GEN_CMD"
  echo "Then resume with:"
  echo "  GEN=done $HERE/pipeline_r51.sh $FAMILY $D"
  exit 0
fi

# (d) choose base's finals, then base's thoughts at tool steps (only where its call matches the scripted call)
[ -e "$D/finished" ] && die "$D/finished exists"
mkdir -p "$D/choose" || exit 1
cd "$T/ask-lora-r5/gpu" || exit 1
"$PY" choose_r5.py --partial "$D/sealed/partial.jsonl" --raw "$D/raw/*.jsonl" --out-chosen "$D/choose/chosen.jsonl" --out-stats "$D/choose/stats.json" \
    --out-nopass-ids "$D/choose/nopass.ids" --out-drop-ids "$D/choose/drop.ids" --out-rejections "$D/choose/rejections.jsonl" > "$D/choose/choose_r5.log" 2>&1 || die "choose_r5: see $D/choose/choose_r5.log"
cat "$D/choose/choose_r5.log" | cut -c1-240
"$PY" choose_traces.py --partial "$D/sealed/partial.jsonl" --raw "$D/raw/*.jsonl" --out-chosen "$D/choose/traces.chosen.jsonl" --out-stats "$D/choose/traces.stats.json" > "$D/choose/choose_traces.log" 2>&1 || die "choose_traces: see $D/choose/choose_traces.log"
cat "$D/choose/choose_traces.log" | cut -c1-400
"$PY" "$HERE/trace_report.py" --partial "$D/sealed/partial.jsonl" --raw "$D/raw/*.jsonl" --out "$D/choose/trace_rejections.jsonl" || die "trace report"
# safety: choose_r5 doesn't judge whether base's final fits the row's intent; unsafe chosen finals are dropped with a rule
"$PY" "$HERE/safety_check_r51.py" --meta "$D/sealed/meta.jsonl" --chosen "$D/choose/chosen.jsonl" --drop "$D/choose/drop.ids" \
    --out "$D/choose/safety_rejections.jsonl" | tee "$D/choose/safety_check.log" || die "safety check"

# (e) finish: splice, drop with a reason per row (drop_manifest.jsonl), validator-flagged base rows dropped too
cd "$T/ask-lora-r5/mi355x" || exit 1
INC=""; [ "${ALLOW_INCOMPLETE:-}" = "1" ] && INC="--allow-incomplete"
env -u WISP_REPO "$PY" finish_r5.py --build "$D/sealed" --chosen "$D/choose/chosen.jsonl" --drop "$D/choose/drop.ids" --traces "$D/choose/traces.chosen.jsonl" \
    --choose-stats "$D/choose/stats.json" --rejections "$D/choose/rejections.jsonl" --wisp-repo "$PIN" --out "$D/finished" $INC > "$D/choose/finish.log" 2>&1
FRC=$?
tail -1 "$D/choose/finish.log" | cut -c1-700
if [ "$FRC" -ne 0 ]; then
  [ -d "$D/finished" ] && mv "$D/finished" "$D/finished.FAILED"
  die "finish_r5 exit $FRC (partial output moved to finished.FAILED; see $D/choose/finish.log)"
fi
cp "$D/seal/"overlap_manifest.*.json "$D/seal/removed_turns.json" "$D/sealed/contract_verification.json" "$D/choose/traces.stats.json" "$D/choose/trace_rejections.jsonl" "$D/finished/" || exit 1

# (f) validate: exit code recorded in validate.log
cd "$T/ask-lora-r2/builder" || exit 1
ASK_LORA_ROUND=4 ASK_LORA_R5=1 ASK_LORA_R51=1 WISP_REPO="$PIN" "$PY" validate.py "$D/finished/all.jsonl" "$D/finished/train.jsonl" "$D/finished/valid.jsonl" > "$D/finished/validate.log" 2>&1
RC=$?
echo "validate exit code: $RC" >> "$D/finished/validate.log"
echo "validate exit $RC (log: $D/finished/validate.log)"
grep -v "Deprecat\|noqa\|WARN\|^  from" "$D/finished/validate.log" | tail -8 | cut -c1-240
exit 0
