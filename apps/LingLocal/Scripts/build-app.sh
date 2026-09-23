#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$APP_DIR/../.." && pwd)"
OUTPUT="${1:-$APP_DIR/dist/Ling Local.app}"
ENGINE_SRC="$REPO_ROOT/tools/ling_engine"

for source in "$ENGINE_SRC/run.sh" "$ENGINE_SRC/cli.py" "$ENGINE_SRC/engine.py"; do
  if [[ ! -f "$source" ]]; then
    echo "Required engine source is missing: $source" >&2
    exit 2
  fi
done
if [[ -e "$OUTPUT" ]]; then
  echo "Refusing to overwrite existing output: $OUTPUT" >&2
  exit 2
fi
PLIST_EXECUTABLE=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP_DIR/Info.plist")
if [[ "$PLIST_EXECUTABLE" != "LingLocal" ]]; then
  echo "Info.plist CFBundleExecutable must be LingLocal; found: $PLIST_EXECUTABLE" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUTPUT")"
STAGING="$(mktemp -d "$(dirname "$OUTPUT")/.LingLocal.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
CONTENTS="$STAGING/Ling Local.app/Contents"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources/ling_engine"
cp "$APP_DIR/Info.plist" "$CONTENTS/Info.plist"
cp "$ENGINE_SRC/run.sh" "$ENGINE_SRC/cli.py" "$ENGINE_SRC/engine.py" "$CONTENTS/Resources/ling_engine/"
chmod 755 "$CONTENTS/Resources/ling_engine/run.sh"
swiftc -O -target arm64-apple-macosx14.0 -framework SwiftUI -framework AppKit \
  "$APP_DIR"/Sources/*.swift -o "$CONTENTS/MacOS/LingLocal"
if [[ ! -x "$CONTENTS/MacOS/$PLIST_EXECUTABLE" ]]; then
  echo "Bundle executable is missing or not executable: $CONTENTS/MacOS/$PLIST_EXECUTABLE" >&2
  exit 2
fi
mv "$STAGING/Ling Local.app" "$OUTPUT"
echo "Built $OUTPUT"
