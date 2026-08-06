#!/usr/bin/env bash
# One-time setup: create the virtualenv Wisp's backend runs in, install
# dependencies, and report on the things Wisp needs that this script cannot
# install for you (oMLX and the model weights).
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${WISP_BOOTSTRAP_PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: '$PYTHON' not found. Install Python 3.10 or newer," >&2
  echo "       or set WISP_BOOTSTRAP_PYTHON to your interpreter." >&2
  exit 1
fi

# Guard the version explicitly. macOS ships a 3.9 stub at /usr/bin/python3 that
# is new enough to run this script and too old to run the backend, so failing
# here with a clear message beats failing later inside an import.
"$PYTHON" - <<'EOF'
import sys
if sys.version_info < (3, 10):
    sys.exit(
        f"error: Python 3.10+ required, found {sys.version.split()[0]} at {sys.executable}.\n"
        "       macOS's /usr/bin/python3 is usually too old — install a newer\n"
        "       Python (e.g. `brew install python`) and re-run."
    )
EOF

if [ ! -d .venv ]; then
  echo "→ creating .venv"
  "$PYTHON" -m venv .venv
else
  echo "→ .venv already exists, reusing it"
fi

echo "→ installing dependencies"
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -e ".[dev]"

echo
echo "✓ backend environment ready."
echo

# --- things this script cannot do for you -----------------------------------

FOUND_OMLX=""
for p in /Applications/oMLX.app "$HOME/Applications/oMLX.app"; do
  [ -d "$p" ] && FOUND_OMLX="$p" && break
done

if [ -n "$FOUND_OMLX" ]; then
  echo "✓ oMLX found at $FOUND_OMLX"
else
  echo "! oMLX not found in /Applications or ~/Applications."
  echo "  Wisp does no inference itself — it talks to oMLX on :8000."
  echo "  Install oMLX and pull the models listed in the README's roster table"
  echo "  before starting Wisp, or the backend will come up with nothing to talk to."
fi

if curl -sf -m 2 http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
  echo "✓ an oMLX server is already responding on :8000"
else
  echo "! nothing is answering on :8000 yet — start oMLX before running Wisp."
fi

echo
echo "Next:"
echo "  scripts/run.sh                       # backend on :8765"
echo "  scripts/package_app.sh && open dist/Wisp.app   # the menu-bar app"
