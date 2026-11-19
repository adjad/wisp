#!/bin/bash
# Inert contract for the production Markdown block parser. Never launches Wisp.
set -euo pipefail
# The code block card holds @State, a macro on recent SDKs (see the helper).
source "$(dirname "$0")/swiftc_nested_sandbox.sh"

project_root=$(cd "$(dirname "$0")/.." && pwd)
markdown_scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-markdown-contract.XXXXXX")
trap 'rm -rf "$markdown_scratch"' EXIT

swiftc ${swiftc_nested_sandbox_flags[@]+"${swiftc_nested_sandbox_flags[@]}"} -parse-as-library -swift-version 5 \
    -target "$(uname -m)-apple-macosx14.0" \
    -module-cache-path "$markdown_scratch/module-cache" \
    "$project_root/app/Sources/WispApp/Theme.swift" \
    "$project_root/app/Sources/WispApp/Math.swift" \
    "$project_root/app/Sources/WispApp/MarkdownView.swift" \
    "$project_root/tests/MarkdownChecks.swift" \
    -o "$markdown_scratch/markdown-checks"

"$markdown_scratch/markdown-checks"
