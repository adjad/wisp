#!/bin/bash
# Run one side of the parity check under /usr/bin/time -l (max RSS), never both at once.
#
#   parity/run_parity.sh reference <model-dir>              -> parity/reference.jsonl
#   parity/run_parity.sh swift <model-dir> <ling-parity>    -> parity/swift.jsonl
#
# Refuses to start when free+inactive memory is below 8 GB or another side is running.
# Optional: LING_REF_PY (interpreter), LING_REF_OUT / LING_SWIFT_OUT (output names);
# LING_REF_OMLX / LING_REF_SITE are read by reference.py.
set -euo pipefail
side="${1:?reference|swift}"
model="${2:?model dir}"
here="$(cd "$(dirname "$0")" && pwd)"

pages=$(vm_stat | awk '/Pages free|Pages inactive|Pages speculative/ {gsub("\\.","",$NF); s+=$NF} END {print s}')
avail_gb=$(( pages * 16384 / 1000000000 ))
if [ "$avail_gb" -lt 8 ]; then
  echo "only ${avail_gb} GB free+inactive; not loading the model" >&2
  exit 3
fi
if pgrep -f "parity/reference.py|ling-parity /" >/dev/null; then
  echo "another parity run is active" >&2
  exit 4
fi

case "$side" in
  reference)
    py="${LING_REF_PY:-/Applications/oMLX.app/Contents/Resources/Python/framework-mlx-base/bin/python_}"
    /usr/bin/time -l "$py" -I "$here/reference.py" "$model" < /dev/null \
      > "$here/${LING_REF_OUT:-reference.jsonl}"
    ;;
  swift)
    tool="${3:?path to ling-parity}"
    /usr/bin/time -l "$tool" "$model" --stream < /dev/null > "$here/${LING_SWIFT_OUT:-swift.jsonl}"
    ;;
  swift-teacher)
    tool="${3:?path to ling-parity}"
    /usr/bin/time -l "$tool" "$model" --teacher "$here/${LING_REF_OUT:-reference.jsonl}" < /dev/null \
      > "$here/${LING_SWIFT_OUT:-swift_teacher.jsonl}"
    ;;
  *) echo "unknown side $side" >&2; exit 2 ;;
esac
