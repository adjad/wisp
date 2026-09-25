#!/bin/bash
# Synthetic native UI/model contract. Never launches Wisp or contacts a service.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
today_scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-today-contract.XXXXXX")
trap 'rm -rf "$today_scratch"' EXIT
export WISP_HOME="$today_scratch/state"
swiftc -parse-as-library -swift-version 5 \
    -module-cache-path "$today_scratch/module-cache" \
    "$project_root/app/Sources/WispApp/TodayView.swift" \
    "$project_root/tests/TodayPlanChecks.swift" \
    -o "$today_scratch/today-checks"
"$today_scratch/today-checks"
