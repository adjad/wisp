#!/bin/bash
# Synthetic decoding/presentation contract for workspace.proactive. Never launches Wisp or contacts a service.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
proactive_scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-proactive-contract.XXXXXX")
trap 'rm -rf "$proactive_scratch"' EXIT
swiftc -parse-as-library -swift-version 5 \
    -module-cache-path "$proactive_scratch/module-cache" \
    "$project_root/app/Sources/WispApp/ProactiveModels.swift" \
    "$project_root/app/Sources/WispApp/NotchDay.swift" \
    "$project_root/app/Sources/WispApp/BriefSummary.swift" \
    "$project_root/tests/ProactiveChecks.swift" \
    -o "$proactive_scratch/proactive-checks"
"$proactive_scratch/proactive-checks"
