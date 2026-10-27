#!/bin/bash
# Pure checks for the first-run (onboarding) window. Never
# launches Wisp, opens a window, or contacts its service.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-first-run-checks.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
sources=()
while IFS= read -r source; do
    sources+=("$source")
done < <(find "$project_root/app/Sources/WispApp" -name '*.swift' ! -name main.swift -print)
# The onboarding rules alone, as build-support/simulation.py's "onboarding" gate compiles them.
swiftc -parse-as-library -swift-version 5 -target "$(uname -m)-apple-macosx14.0" \
    -module-cache-path "$scratch/module-cache" \
    "$project_root/app/Sources/WispApp/Onboarding.swift" \
    "$project_root/tests/OnboardingChecks.swift" \
    -o "$scratch/onboarding-checks"
"$scratch/onboarding-checks"
swiftc -parse-as-library -swift-version 5 -target "$(uname -m)-apple-macosx14.0" \
    -module-cache-path "$scratch/module-cache" \
    "${sources[@]}" \
    "$project_root/tests/FirstRunSetupChecks.swift" \
    "$project_root/tests/WispResetChecks.swift" \
    -lsqlite3 -o "$scratch/first-run-checks"
"$scratch/first-run-checks"
