#!/usr/bin/env bash
# OPTIONAL: deploy the Wisp brain server (behavior engine API) on the GX10.
#
# Installs code into ~/.local/share/wisp-brain/app (previous copy kept as
# app.prev), a venv, a bearer token at ~/.config/wisp-brain/token (mode 0600,
# generated once, never printed), and the user unit wisp-brain.service bound to
# 127.0.0.1:8790. With --serve it also exposes it tailnet-only via
#   tailscale serve --bg --https=8443 http://127.0.0.1:8790
# and verifies the existing :443 gateway mount is unchanged.
#
# Copy the code to the GX10 first (from a checkout on the Mac):
#   rsync -a --relative brain_server service/__init__.py service/behavior deploy/gx10 \
#         asus@100.111.110.43:wisp-brain-src/
# then on the GX10:
#   bash ~/wisp-brain-src/deploy/gx10/install-brain.sh --dry-run
#   bash ~/wisp-brain-src/deploy/gx10/install-brain.sh --serve
#
# Options:
#   --dry-run            print what would happen; change nothing
#   --repo DIR           source checkout (default: two levels above this script)
#   --prefix DIR         install root (default ~/.local/share/wisp-brain)
#   --port N             loopback port (default 8790)
#   --ollama URL         phraser origin (default http://127.0.0.1:11434)
#   --model ID           phraser model (default qwen3.6:35b; keep it the gateway's model
#                        so Ollama does not load a second model)
#   --no-phraser         template text only (no LLM calls from the brain)
#   --tz ZONE            user's IANA timezone (default America/Los_Angeles)
#   --python PATH        interpreter for the venv (default python3)
#   --wheels DIR         install dependencies offline from a wheel directory
#   --serve              also run tailscale serve on https :8443 (refuses if :8443
#                        already serves anything other than http://127.0.0.1:PORT)
#   --no-start           install + enable, do not (re)start
#   -h, --help
set -euo pipefail

# Prints this script's header comment. Pure bash so --help works even where
# only the shell is available (the sandboxed QA allows bash but not awk).
print_header() {
  local line first=1
  while IFS= read -r line; do
    if (( first )); then first=0; continue; fi
    [[ $line == \#* ]] || break
    line=${line#\#}; line=${line# }
    printf '%s\n' "$line"
  done < "$1"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PREFIX="${HOME}/.local/share/wisp-brain"
CONFIG_DIR="${HOME}/.config/wisp-brain"
TOKEN_FILE="${CONFIG_DIR}/token"
STATE_DIR="${HOME}/.local/state/wisp-brain"
UNIT=wisp-brain.service
UNIT_PATH="${HOME}/.config/systemd/user/${UNIT}"
PORT=8790
OLLAMA="http://127.0.0.1:11434"
MODEL="qwen3.6:35b"
PHRASER=1
TZ_NAME="America/Los_Angeles"
PYTHON="python3"
WHEELS=""
SERVE=0
NO_START=0
DRY_RUN=0
# Backup suffix only; fall back rather than abort where `date` is unavailable
# (e.g. the sandboxed QA that runs --help/--dry-run).
TS="$(date +%Y%m%d-%H%M%S 2>/dev/null)" || TS="undated-$$"
PIP_PINS=(fastapi==0.138.1 uvicorn==0.49.0 httpx==0.28.1)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --repo) REPO="$(cd "$2" && pwd)"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --ollama) OLLAMA="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --no-phraser) PHRASER=0; shift ;;
    --tz) TZ_NAME="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --wheels) WHEELS="$2"; shift 2 ;;
    --serve) SERVE=1; shift ;;
    --no-start) NO_START=1; shift ;;
    -h|--help) print_header "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

APP_DIR="${PREFIX}/app"
VENV="${PREFIX}/venv"
DB="${PREFIX}/brain.db"

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run()  {
  if (( DRY_RUN )); then printf '[dry-run] '; printf '%q ' "$@"; printf '\n'; else "$@"; fi
}

# Who owns the tailnet :8443 Serve mount? Prints "absent", "ours" (every handler
# proxies to http://127.0.0.1:${PORT}) or "foreign <what>". Exit 1 when the
# Serve status cannot be read (then nothing on :8443 may be touched).
serve_8443_owner() {
  local status
  status="$(tailscale serve status --json 2>/dev/null)" || return 1
  printf '%s' "$status" | "$PYTHON" -c '
import json, sys
port, want = "8443", "http://127.0.0.1:" + sys.argv[1]
text = sys.stdin.read().strip()
try:
    d = json.loads(text) if text else {}
except ValueError:
    sys.exit(1)
web = {k: v for k, v in (d.get("Web") or {}).items() if k.rsplit(":", 1)[-1] == port}
tcp = (d.get("TCP") or {}).get(port)
if not web and not tcp:
    print("absent"); sys.exit(0)
proxies = [h.get("Proxy") for v in web.values() for h in ((v or {}).get("Handlers") or {}).values()]
if proxies and all(isinstance(x, str) and x.rstrip("/") == want for x in proxies) \
        and not (tcp or {}).get("TCPForward"):
    print("ours"); sys.exit(0)
print("foreign", json.dumps({"web": web, "tcp": tcp}, sort_keys=True)[:300])
' "$PORT"
}

if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/${EUID}" ]]; then
  XDG_RUNTIME_DIR="/run/user/${EUID}"
  export XDG_RUNTIME_DIR
fi

# ---------------------------------------------------------------- preflight
say "Preflight"
[[ -d "${REPO}/brain_server" && -f "${REPO}/service/behavior/engine.py" ]] \
  || die "${REPO} does not contain brain_server/ and service/behavior/ (see --repo)"
command -v "$PYTHON" >/dev/null || die "python not found: $PYTHON"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python >= 3.10 required"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
  || die "python venv/ensurepip missing (Ubuntu: sudo apt install python3-venv)"
command -v curl >/dev/null || die "curl not found"
systemctl --user show-environment >/dev/null 2>&1 || die "systemctl --user is not reachable"
if [[ -n "$(ss -Hltn "sport = :${PORT}" 2>/dev/null)" ]] && ! systemctl --user is-active --quiet "$UNIT"; then
  die "127.0.0.1:${PORT} is in use by something other than ${UNIT}"
fi
SERVE_STATE=""
if (( SERVE )); then
  command -v tailscale >/dev/null || die "tailscale CLI not found"
  # Refuse BEFORE changing anything if :8443 already serves something else.
  SERVE_STATE="$(serve_8443_owner)" \
    || die "cannot read 'tailscale serve status --json'; not touching :8443 (operator permission? see RUNBOOK)"
  case "$SERVE_STATE" in
    absent|ours) say "tailnet :8443 is ${SERVE_STATE}" ;;
    *) die "tailnet :8443 already serves something else (${SERVE_STATE#foreign }); refusing to replace it. Pick another setup or remove that mount yourself." ;;
  esac
fi
if (( PHRASER )); then
  curl -fsS --max-time 3 -o /dev/null "${OLLAMA}/api/version" \
    || warn "Ollama not answering at ${OLLAMA}; the phraser will fall back to templates until it does"
fi

# ---------------------------------------------------------------- code
say "Staging code into ${APP_DIR}"
run mkdir -p "$PREFIX" "$STATE_DIR"
run chmod 700 "$PREFIX" "$STATE_DIR"
NEW="${PREFIX}/app.new"
run rm -rf "$NEW"
run mkdir -p "${NEW}/service"
run cp -R "${REPO}/brain_server" "${NEW}/brain_server"
run cp -R "${REPO}/service/behavior" "${NEW}/service/behavior"
# Empty package marker only: the rest of service/ (macOS-only) is not deployed.
if (( DRY_RUN )); then echo "[dry-run] create ${NEW}/service/__init__.py (empty)"; else : > "${NEW}/service/__init__.py"; fi
run find "$NEW" -name __pycache__ -type d -prune -exec rm -rf {} +
if [[ -d "$APP_DIR" ]]; then
  run rm -rf "${PREFIX}/app.prev"
  run mv "$APP_DIR" "${PREFIX}/app.prev"
fi
run mv "$NEW" "$APP_DIR"

# ---------------------------------------------------------------- venv
if [[ ! -x "${VENV}/bin/python" ]]; then
  say "Creating venv ${VENV}"
  run "$PYTHON" -m venv "$VENV"
fi
say "Installing dependencies: ${PIP_PINS[*]}"
if [[ -n "$WHEELS" ]]; then
  run "${VENV}/bin/pip" install --quiet --no-index --find-links "$WHEELS" "${PIP_PINS[@]}"
else
  run "${VENV}/bin/pip" install --quiet "${PIP_PINS[@]}"
fi
if (( ! DRY_RUN )); then
  (cd "$APP_DIR" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$APP_DIR" "${VENV}/bin/python" -c \
    'import brain_server.app, service.behavior.client, service.behavior.phraser') \
    || die "deployed code does not import; see the error above"
fi

# ---------------------------------------------------------------- token
run mkdir -p "$CONFIG_DIR"
run chmod 700 "$CONFIG_DIR"
if [[ -f "$TOKEN_FILE" ]]; then
  say "Keeping existing token ${TOKEN_FILE}"
  run chmod 600 "$TOKEN_FILE"
else
  say "Generating token ${TOKEN_FILE} (mode 0600, not printed)"
  if (( DRY_RUN )); then
    echo "[dry-run] (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ${TOKEN_FILE})"
  else
    (umask 077 && "$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE")
    chmod 600 "$TOKEN_FILE"
  fi
fi

# ---------------------------------------------------------------- unit
extra="--tz ${TZ_NAME}"
if (( PHRASER )); then extra="--ollama ${OLLAMA} --model ${MODEL} ${extra}"; fi
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
sed -e "s|@APP_DIR@|${APP_DIR}|g" -e "s|@VENV_PY@|${VENV}/bin/python|g" \
    -e "s|@DB@|${DB}|g" -e "s|@TOKEN_FILE@|${TOKEN_FILE}|g" -e "s|@PORT@|${PORT}|g" \
    -e "s|@EXTRA_ARGS@|${extra}|g" "${SCRIPT_DIR}/wisp-brain.service" > "$rendered"
grep -q '@[A-Z0-9_]*@' "$rendered" && die "unrendered placeholder in unit"
if (( DRY_RUN )); then
  echo "----- ${UNIT_PATH} -----"; grep -v '^#' "$rendered" | sed '/^$/d'; echo "-----"
fi
run mkdir -p "$(dirname "$UNIT_PATH")"
[[ -f "$UNIT_PATH" ]] && run cp -p "$UNIT_PATH" "${STATE_DIR}/${UNIT}.bak.${TS}"
run install -m 0644 "$rendered" "$UNIT_PATH"
run systemctl --user daemon-reload
run systemctl --user enable "$UNIT"

if (( NO_START )); then
  say "--no-start: installed and enabled, not started"
else
  run systemctl --user restart "$UNIT"
  if (( ! DRY_RUN )); then
    code=000
    for _ in $(seq 1 30); do
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:${PORT}/healthz" || true)"
      [[ "$code" == 200 || "$code" == 503 ]] && break
      sleep 1
    done
    case "$code" in
      200) say "brain server healthy on 127.0.0.1:${PORT}" ;;
      503) warn "brain server is up but the engine is unavailable (engine not implemented in this build?): journalctl --user -u ${UNIT} -n 50" ;;
      *) die "brain server not answering (HTTP ${code}); see: journalctl --user -u ${UNIT} -n 80" ;;
    esac
    # Authenticated probe without putting the token on a command line.
    hdr="$(mktemp)"
    chmod 600 "$hdr"
    printf 'Authorization: Bearer %s\n' "$(<"$TOKEN_FILE")" > "$hdr"
    scode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H @"$hdr" "http://127.0.0.1:${PORT}/v1/status" || true)"
    rm -f "$hdr"
    say "authenticated /v1/status -> HTTP ${scode}"
  fi
fi

# ---------------------------------------------------------------- tailscale serve :8443
if (( SERVE )); then
  before="${STATE_DIR}/serve-before.${TS}.json"
  after="${STATE_DIR}/serve-after.${TS}.json"
  if (( DRY_RUN )); then
    echo "[dry-run] tailscale serve status --json > ${before}"
    if [[ "$SERVE_STATE" == ours ]]; then
      echo "[dry-run] :8443 already proxies to http://127.0.0.1:${PORT}; leaving it"
    else
      echo "[dry-run] tailscale serve --bg --https=8443 http://127.0.0.1:${PORT}"
    fi
  else
    tailscale serve status --json > "$before" 2>/dev/null || echo '{}' > "$before"
    # Re-check right before writing (the preflight may be minutes old).
    SERVE_STATE="$(serve_8443_owner)" || die "cannot read tailscale serve status; not touching :8443"
    case "$SERVE_STATE" in
      absent|ours) ;;
      *) die "tailnet :8443 now serves something else (${SERVE_STATE#foreign }); refusing to replace it" ;;
    esac
    if [[ "$SERVE_STATE" == ours ]]; then
      say ":8443 already proxies to http://127.0.0.1:${PORT}; leaving it"
    elif ! tailscale serve --bg --https=8443 "http://127.0.0.1:${PORT}"; then
      warn "tailscale serve failed (operator permission?). Run it yourself:"
      warn "  sudo tailscale serve --bg --https=8443 http://127.0.0.1:${PORT}"
    fi
    tailscale serve status --json > "$after" 2>/dev/null || echo '{}' > "$after"
    "$PYTHON" - "$before" "$after" <<'PY'
import json, sys

def load(path):
    try:
        with open(path) as fh:
            text = fh.read().strip()
        return json.loads(text) if text else {}
    except Exception:
        return {}

def port(d, p):
    web = {k: v for k, v in (d.get("Web") or {}).items() if k.endswith(f":{p}")}
    tcp = (d.get("TCP") or {}).get(str(p))
    return {"web": web, "tcp": tcp}

b, a = load(sys.argv[1]), load(sys.argv[2])
if port(b, 443) == port(a, 443):
    print("==> :443 Serve mount unchanged")
else:
    print("WARNING: the :443 Serve mount CHANGED. Compare:", sys.argv[1], sys.argv[2], file=sys.stderr)
    sys.exit(1)
print("==> :8443 now:", json.dumps(port(a, 8443)["web"]) or "(not configured)")
PY
    tailscale serve status || true
  fi
fi

cat <<EOF

Brain server:
  local:    http://127.0.0.1:${PORT}/healthz
  tailnet:  https://<this-host>.ts.net:8443/healthz   (after --serve)
  logs:     journalctl --user -u ${UNIT} -f
  token:    ${TOKEN_FILE} (0600). Move it to the Mac Keychain without echoing it;
            see docs/gx10/RUNBOOK.md "Brain token to the Mac".
Rollback:   bash ${SCRIPT_DIR}/uninstall-brain.sh [--serve-off] [--dry-run]
            (or --restore-previous to go back to ${PREFIX}/app.prev)
EOF
