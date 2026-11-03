#!/bin/bash
# Top-up rounds: for the ids that have no passing sample yet, draw one more sample per round (k = K0, K0+1, ...,
# KMAX-1), re-running choose.py between rounds so an id stops as soon as one sample passes.
#   topup.sh PARTIAL RAW FAMILIES K0 KMAX        (REFS=refs.json for session_chat's recall checks)
# Each round goes through run.sh (heavy lock, oMLX settings restored on exit).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-$HOME/Desktop/MOE_Project/.venv/bin/python}"
PARTIAL="$1"; RAW="$2"; FAMS="$3"; K0="$4"; KMAX="$5"
WORK="${TMPDIR:-/tmp}/r4-distill-topup.$$"; mkdir -p "$WORK"
for ((k = K0; k < KMAX; k++)); do
  "$PY" "$HERE/choose.py" --partial "$PARTIAL" --raw "$RAW" ${TOOL_NAMES:+--tool-names "$TOOL_NAMES"} ${REFS:+--refs "$REFS"} --out-chosen "$WORK/chosen.jsonl" \
    --out-stats "$WORK/stats.json" >/dev/null || exit 1
  "$PY" - "$WORK/stats.json" "$FAMS" > "$WORK/ids.txt" <<'PY'
import json, sys
st, fams = json.load(open(sys.argv[1])), set(sys.argv[2].split(","))
for f, g in st["families"].items():
    if f in fams:
        print("\n".join(g["no_pass"]))
PY
  n=$(grep -c . "$WORK/ids.txt")
  echo "== round k=$k: $n ids with no passing sample"
  [ "$n" -eq 0 ] && break
  "$HERE/run.sh" --partial "$PARTIAL" --families "$FAMS" --ids "$WORK/ids.txt" --k $((k + 1)) --out "$RAW" || exit $?
done
