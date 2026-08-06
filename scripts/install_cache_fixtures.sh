#!/usr/bin/env bash
# Swap in the synthetic Messages/Mail/Notes cache fixtures for manual testing
# — WITHOUT losing your real synced content. Backs up ~/.moe/cache first, then
# copies test_fixtures/cache/*.txt (built by `wisp_testdata.py build-cache`)
# into place. Run scripts/restore_cache_backup.sh afterwards to put your real
# data back.
#
# Heads up: if the real Wisp.app is running (or you start it after this), its
# Swift readers will re-sync and silently overwrite these fixtures on their
# own cadence — messages/mail headers ~5min, mail history ~30min, notes ~24h
# (see the "Sync caches" note in the project's memory). For a clean test
# window, quit Wisp.app first, run this script, restart the backend
# standalone (scripts/run.sh), test, then quit it before restoring.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURES="$ROOT/test_fixtures/cache"
CACHE="$HOME/.moe/cache"
BACKUP="$HOME/.moe/cache_backup_$(date +%Y%m%d_%H%M%S)"

if [ ! -d "$FIXTURES" ] || [ -z "$(ls -A "$FIXTURES" 2>/dev/null)" ]; then
  echo "no fixtures found — run: .venv/bin/python scripts/wisp_testdata.py build-cache" >&2
  exit 1
fi

mkdir -p "$CACHE"
if [ -n "$(ls -A "$CACHE" 2>/dev/null)" ]; then
  mkdir -p "$BACKUP"
  cp -p "$CACHE"/*.txt "$BACKUP"/ 2>/dev/null || true
  echo "✓ backed up your real cache to $BACKUP"
else
  echo "(no existing cache content to back up)"
fi

cp -p "$FIXTURES"/*.txt "$CACHE"/
chmod 600 "$CACHE"/*.txt
echo "✓ installed synthetic Messages/Mail/Notes fixtures into $CACHE"
echo ""
echo "Restart the Wisp backend (or the whole app) for it to pick these up."
echo "When you're done testing:  bash scripts/restore_cache_backup.sh"
