#!/usr/bin/env bash
# Swap in the synthetic Messages/Mail/Notes cache fixtures for manual testing
# — WITHOUT losing your real synced content. Backs up ~/.moe/cache first, then
# copies test_fixtures/cache/*.txt (built by `wisp_testdata.py build-cache`)
# into place. Run scripts/restore_cache_backup.sh afterwards to put your real
# data back.
#
# Running this again before a restore keeps the first backup: it never backs
# up the fixtures themselves, so the restore still returns your real data.
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
# Present while the fixtures are installed; its first line is the backup that
# holds the real cache (empty when there was nothing real to back up).
MARKER="$CACHE/.wisp_fixtures_installed"

if [ ! -d "$FIXTURES" ] || [ -z "$(ls -A "$FIXTURES" 2>/dev/null)" ]; then
  echo "no fixtures found — run: .venv/bin/python scripts/wisp_testdata.py build-cache" >&2
  exit 1
fi

mkdir -p "$CACHE"
KEPT=""
if [ -f "$MARKER" ]; then
  IFS= read -r KEPT < "$MARKER" || true
fi

if [ -n "$KEPT" ] && [ -d "$KEPT" ]; then
  BACKUP="$KEPT"
  echo "(fixtures are already installed; keeping your real cache backup at $BACKUP)"
else
  # Real content is any cache file that is not a byte-for-byte fixture copy.
  REAL=()
  for file in "$CACHE"/*.txt; do
    [ -f "$file" ] || continue
    cmp -s "$file" "$FIXTURES/${file##*/}" 2>/dev/null || REAL+=("$file")
  done
  if [ "${#REAL[@]}" -gt 0 ]; then
    # No `|| true`: a failed backup stops here, before anything is overwritten.
    mkdir -p "$BACKUP"
    cp -p "${REAL[@]}" "$BACKUP"/
    echo "✓ backed up your real cache to $BACKUP"
  else
    BACKUP=""
    echo "(no real cache content to back up)"
  fi
fi

cp -p "$FIXTURES"/*.txt "$CACHE"/
chmod 600 "$CACHE"/*.txt
printf '%s\n' "$BACKUP" > "$MARKER"
echo "✓ installed synthetic Messages/Mail/Notes fixtures into $CACHE"
echo ""
echo "Restart the Wisp backend (or the whole app) for it to pick these up."
echo "When you're done testing:  bash scripts/restore_cache_backup.sh"
