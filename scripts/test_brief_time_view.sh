#!/bin/bash
# Full native rendering with synthetic transports; never starts an AppDelegate.
set -euo pipefail
source "$(dirname "$0")/swiftc_nested_sandbox.sh"
project_root=$(cd "$(dirname "$0")/.." && pwd)
brief_view_scratch=$(mktemp -d "/private/tmp/wisp-brief-view.XXXXXX")
trap 'rm -rf "$brief_view_scratch"' EXIT
export WISP_HOME="$brief_view_scratch/state"
brief_view_output="${WISP_BRIEF_TIME_QA_OUT:-$project_root/app/.build/brief-time-renders}"
export WISP_BRIEF_TIME_QA_OUT="$brief_view_scratch/renders"
export WISP_BRIEF_TIME_QA_ROOT="$brief_view_scratch"
export WISP_BRIEF_TIME_QA_DENIED_RECEIPT="$brief_view_scratch/denied/.moe/receipts.json"
export TMPDIR="$brief_view_scratch/tmp"
mkdir -p "$WISP_HOME" "$TMPDIR" "$brief_view_scratch/denied/.moe"
printf '%s\n' '{"synthetic-only":{"kind":"fixture","done":true}}' > "$WISP_BRIEF_TIME_QA_DENIED_RECEIPT"
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
# Foundation's real home ignores WISP_HOME. Discover its path without starting
# AppKit or constructing a model, then deny all host-home access and networking.
export WISP_BRIEF_TIME_QA_REAL_HOME="$("$brief_view_scratch/brief-view-checks" --print-real-home)"
/usr/bin/sandbox-exec \
    -D BRIEF_REAL_HOME="$WISP_BRIEF_TIME_QA_REAL_HOME" \
    -D BRIEF_DENIED_FIXTURE="$brief_view_scratch/denied" \
    -p '(version 1)
        (allow default)
        (deny network*)
        (deny file-read* file-write* (subpath (param "BRIEF_REAL_HOME")))
        (deny file-read* file-write* (subpath (param "BRIEF_DENIED_FIXTURE")))' \
    "$brief_view_scratch/brief-view-checks"
# Only synthetic renders leave the sandbox, after every assertion succeeds.
mkdir -p "$brief_view_output"
cp "$brief_view_scratch/renders/"*.png "$brief_view_output/"
