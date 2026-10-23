#!/bin/bash
# Synthetic production interactions only; no app delegate, live model or native data.
set -euo pipefail
release_mode=direct
case "${1:-}" in
    "") [[ $# -eq 0 ]] || exit 2 ;;
    --outer-isolation) [[ $# -eq 1 ]] || exit 2; release_mode=outer ;;
    *) printf '%s\n' 'Usage: test_release_interactions.sh [--outer-isolation]' >&2; exit 2 ;;
esac
project_root=$(cd "$(dirname "$0")/.." && pwd)
release_parent=$(cd "${TMPDIR:?Supply an owned fixture TMPDIR}" && pwd -P)
[[ -d "$release_parent" && -O "$release_parent" && "$release_parent" != / ]] || exit 2
release_scratch=$(mktemp -d "$release_parent/wisp-release-interactions-XXXXXX")
trap 'rm -rf "$release_scratch"' EXIT
export WISP_HOME="$(mktemp -d "$release_scratch/state-XXXXXX")"
export WISP_RELEASE_INTERACTIONS_ROOT="$release_scratch"
export WISP_RELEASE_INTERACTIONS_PARENT="$release_parent"
release_denied=$(mktemp -d "$release_scratch/denied-XXXXXX")
export WISP_RELEASE_INTERACTIONS_DENIED="$release_denied/synthetic-control"
export TMPDIR="$(mktemp -d "$release_scratch/tmp-XXXXXX")"
export CLANG_MODULE_CACHE_PATH="$release_scratch/clang"
export SWIFTPM_MODULECACHE_OVERRIDE="$release_scratch/modules"
printf '%s\n' 'Synthetic denied fixture only' > "$WISP_RELEASE_INTERACTIONS_DENIED"
source "$(dirname "$0")/swiftc_nested_sandbox.sh"
release_sources=()
while IFS= read -r -d '' source_file; do
    case "$source_file" in
        app/Sources/WispApp/main.swift) ;;
        app/Sources/WispApp/*.swift) release_sources+=("$project_root/$source_file") ;;
    esac
done < <(git -C "$project_root" ls-files -z -- app/Sources/WispApp)
[[ ${#release_sources[@]} -gt 0 ]] || exit 2
swiftc ${swiftc_nested_sandbox_flags[@]+"${swiftc_nested_sandbox_flags[@]}"} \
    -parse-as-library -swift-version 5 -D WISP_NOTCH_QA \
    -target "$(uname -m)-apple-macosx14.0" \
    -module-cache-path "$release_scratch/module-cache" \
    "${release_sources[@]}" "$project_root/tests/ReleaseInteractionChecks.swift" \
    -lsqlite3 -o "$release_scratch/release-interactions"
export WISP_RELEASE_INTERACTIONS_REAL_HOME="$("$release_scratch/release-interactions" --print-real-home)"
if [[ "$release_mode" == outer ]]; then
    # No nested Seatbelt. The binary must prove the inherited policy itself,
    # starting with real-home denial, before touching production model state.
    "$release_scratch/release-interactions" --outer-isolation
    exit 0
fi
/usr/bin/sandbox-exec \
    -D RELEASE_REAL_HOME="$WISP_RELEASE_INTERACTIONS_REAL_HOME" \
    -D RELEASE_DENIED_FIXTURE="$release_denied" \
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
