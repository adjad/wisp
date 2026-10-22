#!/bin/bash
# Full native rendering with synthetic transports; never starts an AppDelegate.
set -euo pipefail
source "$(dirname "$0")/swiftc_nested_sandbox.sh"
project_root=$(cd "$(dirname "$0")/.." && pwd)
brief_view_scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-brief-view.XXXXXX")
trap 'rm -rf "$brief_view_scratch"' EXIT
export WISP_HOME="$brief_view_scratch/state"
export WISP_BRIEF_TIME_QA_OUT="${WISP_BRIEF_TIME_QA_OUT:-$project_root/app/.build/brief-time-renders}"
brief_view_sources=()
while IFS= read -r -d '' source_file; do
    brief_view_sources+=("$source_file")
done < <(find "$project_root/app/Sources/WispApp" -type f -name '*.swift' ! -name main.swift -print0)
swiftc ${swiftc_nested_sandbox_flags[@]+"${swiftc_nested_sandbox_flags[@]}"} \
    -parse-as-library -swift-version 5 -D WISP_NOTCH_QA \
    -target "$(uname -m)-apple-macosx14.0" \
    -module-cache-path "$brief_view_scratch/module-cache" \
    "${brief_view_sources[@]}" "$project_root/tests/BriefTimeViewChecks.swift" \
    -lsqlite3 -o "$brief_view_scratch/brief-view-checks"
"$brief_view_scratch/brief-view-checks"
