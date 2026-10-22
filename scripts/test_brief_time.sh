#!/bin/bash
# Synthetic presentation contract. Never launches Wisp or contacts a service.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
brief_time_scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-brief-time.XXXXXX")
trap 'rm -rf "$brief_time_scratch"' EXIT
swiftc -parse-as-library -swift-version 5 \
    -module-cache-path "$brief_time_scratch/module-cache" \
    "$project_root/app/Sources/WispApp/NotchDay.swift" \
    "$project_root/app/Sources/WispApp/BriefSummary.swift" \
    "$project_root/app/Sources/WispApp/BriefTimeContext.swift" \
    "$project_root/tests/BriefTimeChecks.swift" \
    -o "$brief_time_scratch/brief-time-checks"
"$brief_time_scratch/brief-time-checks"
