#!/usr/bin/env bash
# One-shot setup for the Air periodic node.
#
# Idempotent — safe to re-run after pulling new code. It does NOT touch
# ~/WispAir/air_service.py (port 8766, the older disabled routing experiment)
# or its launchd plist.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$HOME/WispAir"
STATE="$HOME/.wispair"
PLIST="$HOME/Library/LaunchAgents/com.wisp.air.periodic.plist"

echo "=== Wisp Air periodic node — install ==="

# --- preflight -------------------------------------------------------------
echo
echo "→ preflight"
sw_vers
echo
df -h / | tail -1
echo
if ! command -v swift >/dev/null 2>&1; then
  echo "⚠ swift not found — install Xcode Command Line Tools: xcode-select --install"
  echo "  (the Python service will still work; the reader app needs Swift)"
fi

PY="$(command -v python3)"
echo "→ python: $PY ($("$PY" --version))"

# --- state dir -------------------------------------------------------------
echo
echo "→ state dir $STATE (0700 — holds real mail/message content)"
mkdir -p "$STATE"
chmod 700 "$STATE"

# --- code ------------------------------------------------------------------
echo "→ installing service into $TARGET"
mkdir -p "$TARGET"
cp -R "$ROOT/wispair" "$TARGET/"
cp "$ROOT/air_periodic.py" "$TARGET/"
cp "$ROOT/requirements.txt" "$TARGET/"

# --- venv ------------------------------------------------------------------
if [ ! -d "$TARGET/.venv" ]; then
  echo "→ creating venv"
  "$PY" -m venv "$TARGET/.venv"
fi
echo "→ installing dependencies"
"$TARGET/.venv/bin/pip" install -q --upgrade pip
"$TARGET/.venv/bin/pip" install -q -r "$TARGET/requirements.txt"

# --- api key ---------------------------------------------------------------
KEY="$("$TARGET/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$TARGET')
from wispair import config; print(config.api_key())
")"

# --- launchd ---------------------------------------------------------------
# caffeinate -s mirrors what air_service.py already does: it keeps the machine
# from idle-sleeping so the scheduled runs actually happen. It does NOT defeat
# lid-close sleep — nothing in userspace does — which is exactly why the
# scheduler is written to catch up on wake rather than to rely on a timer.
echo "→ installing launchd agent → $PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.wisp.air.periodic</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-s</string>
    <string>$TARGET/.venv/bin/python</string>
    <string>$TARGET/air_periodic.py</string>
  </array>
  <key>WorkingDirectory</key><string>$TARGET</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$STATE/periodic.log</string>
  <key>StandardErrorPath</key><string>$STATE/periodic.err.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$(id -u)/com.wisp.air.periodic" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3

echo
echo "→ health check"
curl -s --max-time 5 localhost:8767/health >/dev/null \
  && echo "✓ service responding on :8767" \
  || { echo "✗ no response — check $STATE/periodic.err.log"; tail -20 "$STATE/periodic.err.log" 2>/dev/null || true; }

cat <<EOF

=== installed ===

API key (configure this on the Pro):
  $KEY

Remaining steps:
  1. Build + install the reader app:   $ROOT/scripts/package_reader.sh
  2. Open it once so macOS prompts:    open ~/Applications/WispAirReader.app
  3. Grant Full Disk Access manually (it is not promptable):
     System Settings ▸ Privacy & Security ▸ Full Disk Access ▸ + WispAirReader
  4. Add it to Login Items so it starts with the session.
  5. Point the node at your model server if it isn't on :8081 —
     curl -X POST localhost:8767/config -H "X-Wisp-Key: $KEY" \\
       -H 'Content-Type: application/json' \\
       -d '{"model_base_url":"http://127.0.0.1:8081/v1","model_name":"gemma-4-26b-a4b-it"}'

Verify end to end:
  curl -s "localhost:8767/health?deep=1" | python3 -m json.tool
  curl -s -X POST "localhost:8767/run?force=1" -H "X-Wisp-Key: $KEY"
EOF
