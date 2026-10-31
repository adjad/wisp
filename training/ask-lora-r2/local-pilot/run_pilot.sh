#!/bin/bash
# Run the local pilot with a memory watchdog: kills the pilot if free memory drops below 10%.
# Usage: run_pilot.sh <model dir> <data dir> <out dir>
set -u
PY=/Users/adijain/Desktop/MOE_Project/.venv/bin/python
HERE="$(cd "$(dirname "$0")" && pwd)"
"$PY" -W ignore "$HERE/pilot.py" --model "$1" --data "$2" --out "$3" > "$3/pilot.out" 2>&1 &
P=$!
while kill -0 $P 2>/dev/null; do
  FREE=$(memory_pressure -Q | awk -F': ' '/free percentage/{gsub("%","",$2);print $2}')
  echo "$(date +%T) free=${FREE}%" >> "$3/mem.txt"
  if [ "${FREE:-100}" -lt 10 ]; then echo "WATCHDOG: free memory ${FREE}% - killing pilot" | tee -a "$3/pilot.out"; kill $P; fi
  sleep 3
done
wait $P; echo "pilot exit $?" | tee -a "$3/pilot.out"
