#!/bin/bash
# Synthetic contract for the Chat window's pure logic and store. Never launches Wisp or contacts a service.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/wisp-chat-contract.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
chat="$project_root/app/Sources/WispApp/Chat"
swiftc -parse-as-library -swift-version 5 -module-cache-path "$scratch/module-cache" \
    "$chat/ChatTypes.swift" "$project_root/tests/ChatChecks.swift" -o "$scratch/chat-checks"
swiftc -parse-as-library -swift-version 5 -module-cache-path "$scratch/module-cache" \
    "$chat/ChatTypes.swift" "$chat/ChatStore.swift" "$project_root/tests/ChatStoreChecks.swift" -o "$scratch/chat-store-checks"
"$scratch/chat-checks"
"$scratch/chat-store-checks"
