#!/bin/bash
# Synthetic production interactions only; no app delegate, live model or native data.
set -euo pipefail
source "$(dirname "$0")/swiftc_nested_sandbox.sh"
project_root=$(cd "$(dirname "$0")/.." && pwd)
release_scratch=$(mktemp -d /private/tmp/wisp-release-interactions-XXXXXX)
trap 'rm -rf "$release_scratch"' EXIT
export WISP_HOME="$release_scratch/state"
export WISP_RELEASE_INTERACTIONS_ROOT="$release_scratch"
export WISP_RELEASE_INTERACTIONS_DENIED="$release_scratch/denied/synthetic-control"
export TMPDIR="$release_scratch/tmp"
mkdir -p "$WISP_HOME" "$TMPDIR" "$release_scratch/denied"
printf '%s\n' 'Synthetic denied fixture only' > "$WISP_RELEASE_INTERACTIONS_DENIED"
release_sources=()
while IFS= read -r -d '' source_file; do
    release_sources+=("$source_file")
done < <(find "$project_root/app/Sources/WispApp" -type f -name '*.swift' ! -name main.swift -print0)
swiftc ${swiftc_nested_sandbox_flags[@]+"${swiftc_nested_sandbox_flags[@]}"} \
    -parse-as-library -swift-version 5 -D WISP_NOTCH_QA \
    -target "$(uname -m)-apple-macosx14.0" \
    -module-cache-path "$release_scratch/module-cache" \
    "${release_sources[@]}" "$project_root/tests/ReleaseInteractionChecks.swift" \
    -lsqlite3 -o "$release_scratch/release-interactions"
export WISP_RELEASE_INTERACTIONS_REAL_HOME="$("$release_scratch/release-interactions" --print-real-home)"
/usr/bin/sandbox-exec \
    -D RELEASE_REAL_HOME="$WISP_RELEASE_INTERACTIONS_REAL_HOME" \
    -D RELEASE_DENIED_FIXTURE="$release_scratch/denied" \
    -D RELEASE_EXECUTABLE="$release_scratch/release-interactions" \
    -p '(version 1)
        (allow default)
        (deny network*)
        (deny appleevent-send)
        (deny process-exec)
        (allow process-exec (literal (param "RELEASE_EXECUTABLE")))
        (deny file-read* file-write* (subpath (param "RELEASE_REAL_HOME")))
        (deny file-read* file-write* (subpath (param "RELEASE_DENIED_FIXTURE")))' \
    "$release_scratch/release-interactions"
