#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/wisp-reply-contract.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT
swiftc -module-cache-path "$build_dir/module-cache" \
  "$root/app/Sources/WispApp/MailReplyScript.swift" \
  "$root/tests/MailReplyScriptChecks.swift" -o "$build_dir/checks"
"$build_dir/checks"
