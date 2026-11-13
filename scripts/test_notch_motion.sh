#!/bin/bash
# Frame-by-frame motion evidence for the notch panel: the real OverlayView in the
# real OverlayPanel, far off-screen, synthetic transports, no network, no home.
set -euo pipefail
source "$(dirname "$0")/swiftc_nested_sandbox.sh"
project_root=$(cd "$(dirname "$0")/.." && pwd)
motion_scratch=$(mktemp -d "/private/tmp/wisp-notch-motion.XXXXXX")
trap 'rm -rf "$motion_scratch"' EXIT
export WISP_HOME="$motion_scratch/state"
motion_output="${WISP_NOTCH_MOTION_OUT:-$project_root/app/.build/notch-motion}"
export WISP_NOTCH_MOTION_OUT="$motion_scratch/frames"
export TMPDIR="$motion_scratch/tmp"
mkdir -p "$WISP_HOME" "$TMPDIR" "$WISP_NOTCH_MOTION_OUT" "$motion_scratch/denied"
motion_sources=()
while IFS= read -r -d '' source_file; do
    motion_sources+=("$source_file")
done < <(find "$project_root/app/Sources/WispApp" -type f -name '*.swift' ! -name main.swift -print0)
swiftc ${swiftc_nested_sandbox_flags[@]+"${swiftc_nested_sandbox_flags[@]}"} \
    -parse-as-library -swift-version 5 -D WISP_NOTCH_QA \
    -target "$(uname -m)-apple-macosx14.0" \
    -module-cache-path "$motion_scratch/module-cache" \
    "${motion_sources[@]}" "$project_root/tests/NotchMotionChecks.swift" \
    -lsqlite3 -o "$motion_scratch/notch-motion-checks"
# Foundation's real home ignores WISP_HOME. Discover its path without starting
# a panel, then deny all host-home access and networking.
motion_real_home="$("$motion_scratch/notch-motion-checks" --print-real-home)"
status=0
/usr/bin/sandbox-exec \
    -D MOTION_REAL_HOME="$motion_real_home" \
    -D MOTION_DENIED="$motion_scratch/denied" \
    -p '(version 1)
        (allow default)
        (deny network*)
        (deny file-read* file-write* (subpath (param "MOTION_REAL_HOME")))
        (deny file-read* file-write* (subpath (param "MOTION_DENIED")))' \
    "$motion_scratch/notch-motion-checks" || status=$?
# The contact sheets and the report leave the sandbox even on failure: they are
# the evidence. The script still fails when any transition did.
mkdir -p "$motion_output"
cp "$WISP_NOTCH_MOTION_OUT/"*.png "$WISP_NOTCH_MOTION_OUT/"*.txt "$motion_output/" 2>/dev/null || true
echo "Motion evidence: $motion_output"
exit "$status"
