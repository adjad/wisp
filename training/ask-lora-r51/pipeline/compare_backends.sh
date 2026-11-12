#!/bin/bash
# Same prompts, two backends: base Gemma's samples from the Mac's oQ4e (oMLX) vs bf16 on the GPU (gen_finals.py), both
# through the r5.1 pipeline's own raw filter, choose_r5 (finals) and choose_traces (tool-call steps). No training.
#
#   compare_backends.sh BUILD_DIR FINALS_IDS CALLS_IDS OQ4E_RAW BF16_RAW_GLOB OUT
#
# BUILD_DIR is a pipeline OUT with sealed/ (e.g. data/pipe3). Each arm keeps only samples k=0,1 of the listed ids.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)" || exit 1
T="$(cd "$HERE/../.." && pwd)" || exit 1
PY="${PY:-$HOME/Desktop/MOE_Project/.venv/bin/python}"
D="$(cd "${1:?BUILD_DIR}" && pwd)" || exit 1
FI="${2:?FINALS_IDS}"; CI="${3:?CALLS_IDS}"; A="${4:?OQ4E_RAW}"; B="${5:?BF16_RAW_GLOB}"; OUT="${6:?OUT}"
mkdir -p "$OUT" || exit 1
OUT="$(cd "$OUT" && pwd)" || exit 1
FI="$(cd "$(dirname "$FI")" && pwd)/$(basename "$FI")"; CI="$(cd "$(dirname "$CI")" && pwd)/$(basename "$CI")"
die() { echo "FAIL: $1"; exit 1; }

for arm in oq4e bf16; do
  W="$OUT/$arm"; mkdir -p "$W/raw" "$W/raw_safe" "$W/choose" || exit 1
  if [ "$arm" = oq4e ]; then SRC=("$A"); else SRC=($B); fi
  "$PY" - "$FI" "$CI" "$W/raw/base.jsonl" "${SRC[@]}" <<'PYEOF' || exit 1
import json, sys
ids = {l.strip() for f in sys.argv[1:3] for l in open(f) if l.strip()}
seen, n = set(), 0
with open(sys.argv[3], "w") as out:
    for f in sys.argv[4:]:
        for l in open(f):
            d = json.loads(l)
            key = (d["id"], d["k"])
            if d["id"] in ids and d["k"] in (0, 1) and key not in seen:
                seen.add(key); out.write(l if l.endswith("\n") else l + "\n"); n += 1
print(f"{sys.argv[3]}: {n} samples for {len(ids)} ids")
PYEOF
  "$PY" "$HERE/safety_check_r51.py" --meta "$D/sealed/meta.jsonl" --filter-raw "$W/raw/base.jsonl" --out "$W/raw_safe/base.jsonl" \
      > "$W/choose/raw_filter.log" || die "raw filter ($arm)"
  (cd "$T/ask-lora-r5/gpu" && "$PY" choose_r5.py --partial "$D/sealed/partial.jsonl" --raw "$W/raw_safe/*.jsonl" \
      --out-chosen "$W/choose/chosen.jsonl" --out-stats "$W/choose/stats.json" --out-nopass-ids "$W/choose/nopass.ids" \
      --out-drop-ids "$W/choose/drop.ids" --out-rejections "$W/choose/rejections.jsonl" > "$W/choose/choose_r5.log" 2>&1) \
      || die "choose_r5 ($arm): see $W/choose/choose_r5.log"
  (cd "$T/ask-lora-r5/gpu" && "$PY" choose_traces.py --partial "$D/sealed/partial.jsonl" --raw "$W/raw/*.jsonl" \
      --out-chosen "$W/choose/traces.chosen.jsonl" --out-stats "$W/choose/traces.stats.json" > "$W/choose/choose_traces.log" 2>&1) \
      || die "choose_traces ($arm): see $W/choose/choose_traces.log"
  "$PY" "$HERE/trace_report.py" --partial "$D/sealed/partial.jsonl" --raw "$W/raw/*.jsonl" --out "$W/choose/trace_rejections.jsonl" \
      > "$W/choose/trace_report.log" || die "trace report ($arm)"
  "$PY" "$HERE/safety_check_r51.py" --meta "$D/sealed/meta.jsonl" --chosen "$W/choose/chosen.jsonl" --drop "$W/choose/drop.ids" \
      --out "$W/choose/safety_rejections.jsonl" > "$W/choose/safety_check.log" || die "safety check ($arm)"
done
"$PY" "$HERE/compare_backends_report.py" --sealed "$D/sealed" --finals "$FI" --calls "$CI" --out "$OUT" --old-drops "$D/choose/drop.ids"
