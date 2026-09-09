#!/bin/bash
set -euo pipefail
if [[ "${1:-}" != "--live-prepare" ]]; then
  echo "Requires --live-prepare and explicit user authorization; briefly opens and discards Mail reply drafts."
  exit 2
fi
root="$(cd "$(dirname "$0")/.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/wisp-reply-live.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT
swiftc -module-cache-path "$build_dir/module-cache" \
  "$root/app/Sources/WispApp/MailReplyScript.swift" \
  "$root/tests/MailReplyLiveChecks.swift" -o "$build_dir/checks"
"$build_dir/checks" "$@"
