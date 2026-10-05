#!/bin/bash
# Pure report serialization and same-user journal reader, no app/network/native effects.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
diagnostic_scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-diagnostic-contract.XXXXXX")
trap 'rm -rf "$diagnostic_scratch"' EXIT
# Foundation stages atomic writes under an executable-named NSIRD folder.
# Match the runner's existing per-run allowance without broadening the sandbox.
diagnostic_binary="$diagnostic_scratch/${WISP_BUILD_FIXTURE_PREFIX:-wisp-diagnostic}-report-checks"
export WISP_HOME="$diagnostic_scratch/state"
swiftc -parse-as-library -swift-version 5 \
    -module-cache-path "$diagnostic_scratch/module-cache" \
    "$project_root/app/Sources/WispApp/DiagnosticReport.swift" \
    "$project_root/tests/DiagnosticReportChecks.swift" \
    -o "$diagnostic_binary"
"$diagnostic_binary"
