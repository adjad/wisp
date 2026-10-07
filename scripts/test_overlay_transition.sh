#!/bin/bash
# Isolated synthetic panel only; never launches Wisp or contacts its service.
set -euo pipefail
source "$(dirname "$0")/swiftc_nested_sandbox.sh"
project_root=$(cd "$(dirname "$0")/.." && pwd)
motion_scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-motion-checks.XXXXXX")
trap 'rm -rf "$motion_scratch"' EXIT
motion_sources=()
while IFS= read -r source; do
    motion_sources+=("$source")
done < <(find "$project_root/app/Sources/WispApp" -name '*.swift' ! -name main.swift -print)
swiftc ${swiftc_nested_sandbox_flags[@]+"${swiftc_nested_sandbox_flags[@]}"} -parse-as-library -swift-version 5 -target "$(uname -m)-apple-macosx14.0" -D WISP_MOTION_APP_DELEGATE_CHECKS \
    -module-cache-path "$motion_scratch/module-cache" \
    "${motion_sources[@]}" \
    "$project_root/tests/OverlayTransitionChecks.swift" \
    -lsqlite3 -o "$motion_scratch/motion-checks"
"$motion_scratch/motion-checks"
