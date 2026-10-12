#!/usr/bin/env bash
# Remove (or roll back) the optional Wisp brain server on the GX10.
#
# Default: stop + disable wisp-brain.service and remove the unit file. Keeps the
# database, token and code so a reinstall resumes where it left off.
#
# Usage: bash deploy/gx10/uninstall-brain.sh [options]
#   --dry-run            print what would happen; change nothing
#   --serve-off          also remove the tailnet :8443 mount (tailscale serve --https=8443 off),
#                        only if it proxies to this brain (http://127.0.0.1:PORT); never touches :443
#   --port N             the brain's loopback port (default 8790)
#   --python PATH        interpreter used to read tailscale's JSON status (default python3)
#   --restore-previous   instead of uninstalling, swap app.prev back in and restart
#   --purge              also delete ~/.local/share/wisp-brain and ~/.config/wisp-brain
#                        (database + token; the Mac then needs a new token)
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

PREFIX="${HOME}/.local/share/wisp-brain"
CONFIG_DIR="${HOME}/.config/wisp-brain"
STATE_DIR="${HOME}/.local/state/wisp-brain"
UNIT=wisp-brain.service
UNIT_PATH="${HOME}/.config/systemd/user/${UNIT}"
DRY_RUN=0
SERVE_OFF=0
RESTORE=0
PURGE=0
PORT=8790
PYTHON="python3"
# Backup suffix only; fall back rather than abort where `date` is unavailable
# (e.g. the sandboxed QA that runs --help/--dry-run).
TS="$(date +%Y%m%d-%H%M%S 2>/dev/null)" || TS="undated-$$"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --serve-off) SERVE_OFF=1; shift ;;
    --restore-previous) RESTORE=1; shift ;;
    --purge) PURGE=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    -h|--help) print_header "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '==> %s\n' "$*"; }
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

if (( RESTORE )); then
  [[ -d "${PREFIX}/app.prev" ]] || die "no previous version at ${PREFIX}/app.prev"
  say "Restoring previous brain server code"
  run mv "${PREFIX}/app" "${PREFIX}/app.rolledback.${TS}"
  run mv "${PREFIX}/app.prev" "${PREFIX}/app"
  run systemctl --user restart "$UNIT"
  say "Restored. Check: curl -s http://127.0.0.1:${PORT}/healthz"
  exit 0
fi

say "Stopping and disabling ${UNIT}"
run systemctl --user stop "$UNIT" 2>/dev/null || true
run systemctl --user disable "$UNIT" 2>/dev/null || true
run systemctl --user reset-failed "$UNIT" 2>/dev/null || true
if [[ -f "$UNIT_PATH" ]]; then
  run mkdir -p "$STATE_DIR"
  run mv "$UNIT_PATH" "${STATE_DIR}/${UNIT}.removed.${TS}"
fi
run systemctl --user daemon-reload

if (( SERVE_OFF )); then
  say "Removing the tailnet :8443 mount if it is ours (the :443 gateway mount is not touched)"
  if ! command -v tailscale >/dev/null; then
    printf 'WARNING: tailscale CLI not found; :8443 left as is\n' >&2
  elif ! state="$(serve_8443_owner)"; then
    printf 'WARNING: cannot read tailscale serve status; :8443 left as is\n' >&2
  elif [[ "$state" == absent ]]; then
    say ":8443 is not configured; nothing to remove"
  elif [[ "$state" != ours ]]; then
    printf 'WARNING: :8443 serves something other than http://127.0.0.1:%s (%s); NOT removing it\n' \
      "$PORT" "${state#foreign }" >&2
  elif ! run tailscale serve --https=8443 off; then
    printf 'If that failed for permissions, run: sudo tailscale serve --https=8443 off\n' >&2
  fi
  run tailscale serve status || true
fi

if (( PURGE )); then
  say "Purging data and token"
  run rm -rf "$PREFIX" "$CONFIG_DIR"
else
  say "Kept data (${PREFIX}) and token (${CONFIG_DIR}); use --purge to delete them"
fi
say "Done. A Mac still in brain.mode remote uses its local engine (retrying remote every 120 s); set it back to local."
