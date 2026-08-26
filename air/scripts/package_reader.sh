#!/usr/bin/env bash
# Build WispAirReader.app — the signed bundle that holds the TCC grants.
#
# It MUST be an .app, not a bare binary or a script: macOS attributes privacy
# permissions to a signed bundle, and Mail Automation in particular only ever
# prompts properly for one with NSAppleEventsUsageDescription in its Info.plist.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
BUNDLE="$DIST/WispAirReader.app"
DEST="$HOME/Applications/WispAirReader.app"

echo "→ building release binary…"
( cd "$ROOT" && swift build -c release )
BIN="$ROOT/.build/release/WispAirReader"
[ -f "$BIN" ] || { echo "build failed: $BIN missing"; exit 1; }

echo "→ assembling $BUNDLE …"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS"
cp "$BIN" "$BUNDLE/Contents/MacOS/WispAirReader"

cat > "$BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>WispAirReader</string>
  <key>CFBundleDisplayName</key><string>Wisp Air Reader</string>
  <key>CFBundleIdentifier</key><string>com.wisp.air.reader</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>WispAirReader</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSRemindersFullAccessUsageDescription</key><string>Wisp Air writes the to-dos it finds in your mail and messages into Reminders, so they reach your other devices.</string>
  <key>NSRemindersUsageDescription</key><string>Wisp Air writes the to-dos it finds in your mail and messages into Reminders, so they reach your other devices.</string>
  <key>NSAppleEventsUsageDescription</key><string>Wisp Air reads your Mail inbox so it can summarize what arrived while you were away.</string>
</dict>
</plist>
PLIST

# Sign with a STABLE identity when one exists. macOS ties TCC grants to the
# code signature, and an ad-hoc signature (--sign -) changes the CDHash on
# EVERY build — which silently wipes all grants on each rebuild. This bit the
# user repeatedly on the Pro before a self-signed cert fixed it there.
#
# One-time setup (the user has to do this; it's a security setting):
#   Keychain Access ▸ Certificate Assistant ▸ Create a Certificate…
#   Name: "WispAir Dev", Identity Type: Self-Signed Root, Type: Code Signing
#   Then set it to Always Trust.
if security find-identity -v -p codesigning 2>/dev/null | grep -q "WispAir Dev"; then
  codesign --force --deep --sign "WispAir Dev" "$BUNDLE"
  echo "✓ signed with 'WispAir Dev' — TCC grants persist across rebuilds"
else
  codesign --force --deep --sign - "$BUNDLE" 2>/dev/null || echo "(ad-hoc codesign skipped)"
  echo "⚠ AD-HOC SIGNATURE — macOS will DROP Mail/Reminders/Full-Disk grants on every rebuild."
  echo "  Create a 'WispAir Dev' self-signed code-signing cert in Keychain Access to fix."
  echo "  (See the comment block in this script for the exact steps.)"
fi

mkdir -p "$HOME/Applications"
rm -rf "$DEST" 2>/dev/null || true
cp -R "$BUNDLE" "$DEST"
echo "✓ installed → $DEST"
echo
echo "Next: open it once so macOS shows the permission prompts —"
echo "  open \"$DEST\""
