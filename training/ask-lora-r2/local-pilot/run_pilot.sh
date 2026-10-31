#!/bin/bash
# Run the local pilot with a memory watchdog: kills the pilot if free memory drops below 10%.
# Usage: run_pilot.sh <gemma|bolt> <model dir> <data dir> <out dir>
# Results go to <out dir>/<tag>, so the Gemma and Bolt pilots never overwrite each other.
# Run ONE pilot at a time (each loads a model).
set -u
[ $# -eq 4 ] || { sed -n '3,4p' "$0"; exit 1; }
case "$1" in gemma|bolt) ;; *) echo "FAIL: first argument must be gemma or bolt"; exit 1 ;; esac
TAG="$1"; MODEL="$2"; DATA="$3"; OUT="$4/$1"
PY=/Users/adijain/Desktop/MOE_Project/.venv/bin/python
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT"
"$PY" -W ignore "$HERE/pilot.py" --tag "$TAG" --model "$MODEL" --data "$DATA" --out "$4" > "$OUT/pilot.out" 2>&1 &
P=$!
while kill -0 $P 2>/dev/null; do
  FREE=$(memory_pressure -Q | awk -F': ' '/free percentage/{gsub("%","",$2);print $2}')
  echo "$(date +%T) free=${FREE}%" >> "$OUT/mem.txt"
  if [ "${FREE:-100}" -lt 10 ]; then echo "WATCHDOG: free memory ${FREE}% - killing pilot" | tee -a "$OUT/pilot.out"; kill $P; fi
  sleep 3
done
wait $P; echo "pilot exit $?" | tee -a "$OUT/pilot.out"
