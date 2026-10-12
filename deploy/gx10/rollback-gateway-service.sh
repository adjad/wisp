#!/usr/bin/env bash
# Roll back GX10 gateway supervision to the previous manual setup.
#
# 1. stops + disables wisp-gx10-gateway.service (user unit)
# 2. moves the unit file into ~/.local/state/wisp-gx10-supervision/ (unless --keep-unit-file)
# 3. relaunches the gateway exactly as it was launched before (recorded
#    /proc/<pid>/cmdline + cwd from the install), detached with setsid/nohup,
#    unless --no-relaunch or something already listens on the port
#
# Never touches tokens.json, the Tailscale Serve :443 mount, Ollama, or linger
# (prints how to disable linger if the install enabled it).
#
# Usage: bash deploy/gx10/rollback-gateway-service.sh [--dry-run] [--no-relaunch] [--keep-unit-file]
#   --dry-run          print what would happen; change nothing
#   --no-relaunch      do not start the manual gateway afterwards
#   --keep-unit-file   leave the (disabled) unit file in place
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

UNIT=wisp-gx10-gateway.service
UNIT_PATH="${HOME}/.config/systemd/user/${UNIT}"
STATE_DIR="${HOME}/.local/state/wisp-gx10-supervision"
DRY_RUN=0
RELAUNCH=1
KEEP_UNIT=0
# Backup suffix only; fall back rather than abort where `date` is unavailable
# (e.g. the sandboxed QA that runs --help/--dry-run).
TS="$(date +%Y%m%d-%H%M%S 2>/dev/null)" || TS="undated-$$"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --no-relaunch) RELAUNCH=0; shift ;;
    --keep-unit-file) KEEP_UNIT=1; shift ;;
    -h|--help) print_header "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run()  {
  if (( DRY_RUN )); then printf '[dry-run] '; printf '%q ' "$@"; printf '\n'; else "$@"; fi
}

if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/${EUID}" ]]; then
  XDG_RUNTIME_DIR="/run/user/${EUID}"
  export XDG_RUNTIME_DIR
fi

# Defaults = the documented manual launch; overridden by the recorded install state.
python="$(command -v python3 || echo /usr/bin/python3)"
gateway="${HOME}/.local/share/wisp-gx10-demo/20261010-full-tools-preview-v2/gateway.py"
tokens="${HOME}/.config/wisp-gx10-demo/20261010-v1/tokens.json"
model="qwen3.6:35b"
port=8767
manual_cwd=""
linger_enabled_by_install=""
if [[ -f "${STATE_DIR}/last-install.env" ]]; then
  while IFS='=' read -r key value; do
    case "$key" in
      python) python="$value" ;;
      gateway) gateway="$value" ;;
      tokens) tokens="$value" ;;
      model) model="$value" ;;
      port) port="$value" ;;
      manual_cwd) manual_cwd="$value" ;;
      linger_enabled_by_install) linger_enabled_by_install="$value" ;;
    esac
  done < "${STATE_DIR}/last-install.env"
fi

port_busy() { [[ -n "$(ss -Hltn "sport = :${port}" 2>/dev/null)" ]]; }

say "Stopping and disabling ${UNIT}"
run systemctl --user stop "$UNIT" 2>/dev/null || true
run systemctl --user disable "$UNIT" 2>/dev/null || true
run systemctl --user reset-failed "$UNIT" 2>/dev/null || true
if [[ -f "$UNIT_PATH" ]] && (( ! KEEP_UNIT )); then
  run mkdir -p "$STATE_DIR"
  run mv "$UNIT_PATH" "${STATE_DIR}/${UNIT}.removed.${TS}"
fi
run systemctl --user daemon-reload

if (( ! RELAUNCH )); then
  say "--no-relaunch: the gateway is NOT running now. Send on the Mac will fail until it is started."
elif (( ! DRY_RUN )) && port_busy; then
  say "Something already listens on 127.0.0.1:${port}; not relaunching"
else
  cmd=()
  if [[ -f "${STATE_DIR}/manual-cmdline.bin" ]]; then
    while IFS= read -r -d '' arg; do cmd+=("$arg"); done < "${STATE_DIR}/manual-cmdline.bin"
  fi
  if (( ${#cmd[@]} == 0 )); then
    cmd=("$python" -B "$gateway" --tokens "$tokens" --model "$model")
  elif [[ "${cmd[0]}" != /* ]]; then
    cmd[0]="$python"   # recorded argv[0] was "python3"; use the recorded absolute interpreter
  fi
  cwd="${manual_cwd:-$(dirname "$gateway")}"
  log="${STATE_DIR}/gateway-manual.log"
  say "Relaunching the manual gateway (cwd ${cwd}, log ${log}):"
  printf '    %q' "${cmd[@]}"; printf '\n'
  if (( DRY_RUN )); then
    echo "[dry-run] (cd ${cwd} && setsid nohup <cmd> >>${log} 2>&1 </dev/null &)"
  else
    mkdir -p "$STATE_DIR"
    # The subshell execs into the gateway: no shell lingers holding our stdout.
    (
      cd "$cwd" || exit 1
      umask 077
      exec setsid nohup "${cmd[@]}"
    ) </dev/null >>"$log" 2>&1 &
    disown || true
    for _ in $(seq 1 30); do port_busy && break; sleep 1; done
    if port_busy; then
      say "Manual gateway is listening on 127.0.0.1:${port}"
    else
      die "manual gateway did not start listening within 30 s; see ${log}"
    fi
  fi
fi

if [[ -n "$linger_enabled_by_install" ]]; then
  say "The install enabled lingering. To restore exactly (only if nothing else needs it): loginctl disable-linger $(id -un)"
fi
say "Rollback complete. Note: a manually launched gateway does NOT survive a reboot."
