#!/bin/bash
# Pure layout and hover-intent checks for the notch/monitor overlay. Never
# launches Wisp, opens a window, or contacts its service.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-display-checks.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
sources=()
while IFS= read -r source; do
    sources+=("$source")
done < <(find "$project_root/app/Sources/WispApp" -name '*.swift' ! -name main.swift -print)
swiftc -parse-as-library -swift-version 5 -target "$(uname -m)-apple-macosx14.0" \
    -module-cache-path "$scratch/module-cache" \
    "${sources[@]}" \
    "$project_root/tests/DisplayGeometryChecks.swift" \
    -lsqlite3 -o "$scratch/display-checks"
"$scratch/display-checks"
