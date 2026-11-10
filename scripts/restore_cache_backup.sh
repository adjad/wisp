#!/usr/bin/env bash
# Restore ~/.moe/cache from the backup install_cache_fixtures.sh recorded when
# it installed the fixtures, or else from the most recent backup (or a
# specific one: pass its full path as $1).
set -euo pipefail
CACHE="$HOME/.moe/cache"
MARKER="$CACHE/.wisp_fixtures_installed"

BACKUP=""
if [ "${1:-}" != "" ]; then
  BACKUP="$1"
else
  if [ -f "$MARKER" ]; then
    IFS= read -r BACKUP < "$MARKER" || true
  fi
  if [ -z "$BACKUP" ] || [ ! -d "$BACKUP" ]; then
    BACKUP="$(ls -dt "$HOME"/.moe/cache_backup_* 2>/dev/null | head -1 || true)"
  fi
fi

if [ -z "${BACKUP:-}" ] || [ ! -d "$BACKUP" ]; then
  echo "no cache backup found under ~/.moe/cache_backup_* — nothing to restore." >&2
  echo "(if you never ran install_cache_fixtures.sh, your real cache was never touched.)" >&2
  exit 1
fi

HAS_FILES=""
for file in "$BACKUP"/*.txt; do
  if [ -f "$file" ]; then HAS_FILES=1; break; fi
done
if [ -z "$HAS_FILES" ]; then
  echo "the backup at $BACKUP holds no .txt cache files — nothing was restored." >&2
  exit 1
fi

mkdir -p "$CACHE"
cp -p "$BACKUP"/*.txt "$CACHE"/
chmod 600 "$CACHE"/*.txt
rm -f "$MARKER"
echo "✓ restored real cache from $BACKUP"
echo "Restart the Wisp backend (or the whole app) for it to pick this up."
echo ""
echo "Once you've confirmed it looks right, you can remove the backup dir:"
echo "  rm -rf \"$BACKUP\""
