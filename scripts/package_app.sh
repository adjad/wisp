#!/usr/bin/env bash
# Compatibility entry point. Produces verified artifacts; never installs an app.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
exec "$root/scripts/wisp-build" all "$@"
