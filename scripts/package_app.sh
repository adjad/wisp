#!/usr/bin/env bash
# Build Wisp.app — a proper menu-bar app bundle you can launch from Spotlight,
# add to Login Items, and relaunch after quitting.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APPDIR="$ROOT/app"
DIST="$ROOT/dist"
BUNDLE="$DIST/Wisp.app"

echo "→ building release binary…"
( cd "$APPDIR" && swift build -c release )
BIN="$APPDIR/.build/release/WispApp"
[ -f "$BIN" ] || { echo "build failed: $BIN missing"; exit 1; }

echo "→ building app icon…"
mkdir -p "$DIST"
( cd "$DIST" && swift "$ROOT/scripts/make_icon.swift" Wisp.iconset >/dev/null \
  && iconutil -c icns Wisp.iconset -o AppIcon.icns )

echo "→ assembling $BUNDLE …"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cp "$BIN" "$BUNDLE/Contents/MacOS/Wisp"
[ -f "$DIST/AppIcon.icns" ] && cp "$DIST/AppIcon.icns" "$BUNDLE/Contents/Resources/AppIcon.icns"

echo "→ bundling release changelog…"
DOCS="$BUNDLE/Contents/Resources/Documentation"
mkdir -p "$DOCS"
cp "$ROOT/ROUTER_REMEDIATION_CHANGELOG.md" "$DOCS/ROUTER_REMEDIATION_CHANGELOG.md"

echo "→ bundling local backend…"
BACKEND="$BUNDLE/Contents/Resources/backend"
mkdir -p "$BACKEND"
cp -R "$ROOT/service" "$BACKEND/service"

echo "→ building runtime venv (backend deps only — no mlx/transformers, oMLX \
serves inference over HTTP; see requirements-runtime.txt)…"
BACKEND_VENV="$BACKEND/.venv"
rm -rf "$BACKEND_VENV"
python3 -m venv "$BACKEND_VENV"
"$BACKEND_VENV/bin/pip" install --quiet --upgrade pip
"$BACKEND_VENV/bin/pip" install --quiet -r "$ROOT/requirements-runtime.txt"
cat > "$BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Wisp</string>
  <key>CFBundleDisplayName</key><string>Wisp</string>
  <key>CFBundleIdentifier</key><string>com.wisp.assistant</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>Wisp</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSCalendarsFullAccessUsageDescription</key><string>Wisp reads your calendar to show upcoming events and remind you before they start.</string>
  <key>NSCalendarsUsageDescription</key><string>Wisp reads your calendar to show upcoming events and remind you before they start.</string>
  <key>NSRemindersFullAccessUsageDescription</key><string>Wisp mirrors the reminders you set into the macOS Reminders app.</string>
  <key>NSRemindersUsageDescription</key><string>Wisp mirrors the reminders you set into the macOS Reminders app.</string>
  <key>NSAppleEventsUsageDescription</key><string>Wisp uses this to read your Mail inbox and Notes for summaries, to send email and messages you approve, and to run scripts you approve in Terminal.</string>
  <key>NSContactsUsageDescription</key><string>Wisp reads your contacts only to show names instead of phone numbers when it talks about your messages.</string>
</dict>
</plist>
PLIST
# Sign with a STABLE identity when one exists ("Wisp Dev" self-signed cert in
# the login keychain): macOS TCC ties permission grants (Calendar, Full Disk
# Access, Automation, …) to the code signature, and an ad-hoc signature changes
# every build — which silently WIPES all grants on each repackage. One-time
# setup: Keychain Access ▸ Certificate Assistant ▸ Create a Certificate…,
# name "Wisp Dev", Identity Type: Self-Signed Root, Certificate Type: Code
# Signing. After that, rebuilds keep their permissions.
if security find-identity -v -p codesigning 2>/dev/null | grep -q "Wisp Dev"; then
  codesign --force --deep --sign "Wisp Dev" "$BUNDLE" && echo "✓ signed with Wisp Dev (TCC permissions persist across rebuilds)"
else
  codesign --force --deep --sign - "$BUNDLE" 2>/dev/null || echo "(ad-hoc codesign skipped)"
  echo "⚠ ad-hoc signature: macOS will DROP granted permissions on every rebuild."
  echo "  Create a 'Wisp Dev' code-signing cert in Keychain Access to fix (see comment above)."
fi

# Install where Spotlight can find it (no sudo needed under ~/Applications).
# Remove any existing bundle first, else cp -R nests inside it.
DEST="/Applications/Wisp.app"
rm -rf "$DEST" 2>/dev/null || true
if cp -R "$BUNDLE" "$DEST" 2>/dev/null; then :; else
  mkdir -p "$HOME/Applications"; DEST="$HOME/Applications/Wisp.app"
  rm -rf "$DEST"; cp -R "$BUNDLE" "$DEST"
fi
echo "✓ installed: $DEST"
echo "  launch with:  open \"$DEST\"   (or find \"Wisp\" in Spotlight)"
