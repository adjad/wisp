#!/bin/bash
# Isolated synthetic panel only; never launches Wisp or contacts its service.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
motion_scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-motion-checks.XXXXXX")
trap 'rm -rf "$motion_scratch"' EXIT
swiftc -parse-as-library -swift-version 5 \
    -module-cache-path "$motion_scratch/module-cache" \
    "$project_root/app/Sources/WispApp/OverlayTransition.swift" \
    "$project_root/app/Sources/WispApp/OverlayPanel.swift" \
    "$project_root/tests/OverlayTransitionChecks.swift" \
    -o "$motion_scratch/motion-checks"
"$motion_scratch/motion-checks"
