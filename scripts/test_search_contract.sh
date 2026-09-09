#!/bin/bash
# Compile and exercise the actual search state model with inert dependencies.
# This never launches Wisp, reads a source app, or contacts a server.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
search_scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-search-contract.XXXXXX")
trap 'rm -rf "$search_scratch"' EXIT
export WISP_HOME="$search_scratch/state"
export CLANG_MODULE_CACHE_PATH="$search_scratch/clang-cache"
swiftc -parse-as-library -swift-version 5 \
    -module-cache-path "$search_scratch/module-cache" \
    "$project_root/app/Sources/WispApp/SearchModel.swift" \
    "$project_root/tests/SearchModelChecks.swift" \
    -o "$search_scratch/search-checks"
"$search_scratch/search-checks"
