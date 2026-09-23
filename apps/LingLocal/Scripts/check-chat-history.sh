#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT="$(mktemp /tmp/ling-chat-history.XXXXXX)"
trap 'rm -f "$OUTPUT"' EXIT

swiftc -swift-version 6 -target arm64-apple-macosx14.0 -framework Foundation \
  "$APP_DIR/Sources/Models.swift" \
  "$APP_DIR/Sources/ChatHistory.swift" \
  "$APP_DIR/Tests/ChatHistoryChecks.swift" \
  -o "$OUTPUT"
"$OUTPUT"
