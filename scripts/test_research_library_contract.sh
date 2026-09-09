#!/bin/bash
# Compile real app models against an inert research transport and store fixtures.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
library_scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-library-contract.XXXXXX")
trap 'rm -rf "$library_scratch"' EXIT
export WISP_HOME="$library_scratch/state"
export PYTHONDONTWRITEBYTECODE=1
export CLANG_MODULE_CACHE_PATH="$library_scratch/clang-cache"
test_python=${WISP_TEST_PYTHON:-"$project_root/.venv/bin/python"}
if [[ ! -x "$test_python" ]]; then test_python=python3; fi
"$test_python" -B "$project_root/tests/research_library_fixtures.py" "$library_scratch/fixtures.json"
swiftc -parse-as-library -swift-version 5 \
    -module-cache-path "$library_scratch/module-cache" \
    "$project_root/app/Sources/WispApp/WispClient.swift" \
    "$project_root/app/Sources/WispApp/ResearchModel.swift" \
    "$project_root/app/Sources/WispApp/ResearchLibraryModel.swift" \
    "$project_root/app/Sources/WispApp/ResearchLibraryView.swift" \
    "$project_root/app/Sources/WispApp/Theme.swift" \
    "$project_root/tests/ResearchLibraryChecks.swift" \
    -o "$library_scratch/library-checks"
"$library_scratch/library-checks" "$library_scratch/fixtures.json"
