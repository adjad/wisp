#!/usr/bin/env bash
# Install systemd USER supervision for the existing Wisp GX10 gateway.
#
# Runs the EXACT existing command (same interpreter, gateway.py, tokens.json,
# model) under ~/.config/systemd/user/wisp-gx10-gateway.service with
# Restart=on-failure, so a reboot or crash no longer leaves Send dead.
#
# Never touches: tokens.json contents/permissions, the Tailscale Serve :443
# mount, Ollama, or any other unit. Never runs two gateways: if something
# already listens on 127.0.0.1:8767 it installs + enables the unit (so the NEXT
# boot is covered) but does not start it, unless --takeover is given.
#
# Usage (on the GX10 as asus):
#   bash deploy/gx10/install-gateway-service.sh --dry-run
#   bash deploy/gx10/install-gateway-service.sh                 # install+enable; start only if port is free
#   bash deploy/gx10/install-gateway-service.sh --takeover      # replace the manual process (~2-5 s gap)
#   bash deploy/gx10/install-gateway-service.sh --enable-linger # also start at boot without login
#
# Options:
#   --dry-run              print what would happen; change nothing
#   --takeover             stop the manually launched gateway (exact path match,
#                          own user only) and start the unit; auto-relaunches the
#                          manual command if the unit fails its health check
#   --no-start             install + enable only
#   --restart              restart the unit if it is already running it
#   --enable-linger        run `loginctl enable-linger $USER` (may need sudo)
#   --gateway PATH         gateway.py (default: the 20261010-full-tools-preview-v2 path)
#   --tokens PATH          tokens.json (default: the 20261010-v1 path)
#   --model ID             default qwen3.6:35b
#   --python PATH          interpreter (default: the running gateway's, else python3)
#   --port N               gateway listen port (default 8767)
#   --expected-sha256 HEX  required sha256 prefix of gateway.py (default eb7080b3)
#   --allow-hash-mismatch  proceed although gateway.py's hash differs
#   --verify-timeout N     seconds to wait for the unit to answer (default 60)
#   -h, --help
set -euo pipefail

UNIT=wisp-gx10-gateway.service
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY="${HOME}/.local/share/wisp-gx10-demo/20261010-full-tools-preview-v2/gateway.py"
TOKENS="${HOME}/.config/wisp-gx10-demo/20261010-v1/tokens.json"
MODEL="qwen3.6:35b"
PYTHON=""
PORT=8767
EXPECTED_SHA="eb7080b3"
DRY_RUN=0
TAKEOVER=0
NO_START=0
RESTART=0
ENABLE_LINGER=0
ALLOW_HASH_MISMATCH=0
VERIFY_TIMEOUT=60

UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_PATH="${UNIT_DIR}/${UNIT}"
SUPPORT_DIR="${HOME}/.local/share/wisp-gx10-supervision"
STATE_DIR="${HOME}/.local/state/wisp-gx10-supervision"
WAIT_SCRIPT="${SUPPORT_DIR}/wait-for-ollama.sh"
# Backup suffix only; fall back rather than abort where `date` is unavailable
# (e.g. the sandboxed QA that runs --help/--dry-run).
TS="$(date +%Y%m%d-%H%M%S 2>/dev/null)" || TS="undated-$$"

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

usage() { print_header "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --takeover) TAKEOVER=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --restart) RESTART=1; shift ;;
    --enable-linger) ENABLE_LINGER=1; shift ;;
    --gateway) GATEWAY="$2"; shift 2 ;;
    --tokens) TOKENS="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --expected-sha256) EXPECTED_SHA="$2"; shift 2 ;;
    --allow-hash-mismatch) ALLOW_HASH_MISMATCH=1; shift ;;
    --verify-timeout) VERIFY_TIMEOUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run()  {
  if (( DRY_RUN )); then printf '[dry-run] '; printf '%q ' "$@"; printf '\n'; else "$@"; fi
}

# systemctl --user over SSH needs the user's runtime dir.
if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/${EUID}" ]]; then
  XDG_RUNTIME_DIR="/run/user/${EUID}"
  export XDG_RUNTIME_DIR
fi

listener_pid() {
  # PID of the process listening on 127.0.0.1:$PORT (own processes only), or empty.
  ss -Hltnp "sport = :${PORT}" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2 || true
}
port_busy() { [[ -n "$(ss -Hltn "sport = :${PORT}" 2>/dev/null)" ]]; }
http_code() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}/v1/models" 2>/dev/null || true
}
unit_main_pid() { systemctl --user show -p MainPID --value "$UNIT" 2>/dev/null || echo 0; }
unit_owns_pid() {
  # true if $1 is the unit's main PID or a descendant of it
  local pid="$1" main
  main="$(unit_main_pid)"
  [[ -n "$pid" && "$main" != "0" && -n "$main" ]] || return 1
  while [[ -n "$pid" && "$pid" != "0" && "$pid" != "1" ]]; do
    [[ "$pid" == "$main" ]] && return 0
    pid="$(awk '/^PPid:/{print $2}' "/proc/${pid}/status" 2>/dev/null || true)"
  done
  return 1
}
cmdline_of() { tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null || true; }

print_rollback() {
  cat <<EOF

Rollback (removes supervision, relaunches the manual gateway command):
  bash ${SCRIPT_DIR}/rollback-gateway-service.sh            # add --dry-run to preview
Logs:    journalctl --user -u ${UNIT} -f
Status:  systemctl --user status ${UNIT}
EOF
}

# ---------------------------------------------------------------- preflight
say "Preflight"
command -v systemctl >/dev/null || die "systemctl not found"
command -v ss >/dev/null || die "ss (iproute2) not found"
command -v curl >/dev/null || die "curl not found"
systemctl --user show-environment >/dev/null 2>&1 \
  || die "systemctl --user is not reachable (log in via ssh as $(id -un); XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-unset})"
[[ -f "$GATEWAY" ]] || die "gateway not found: $GATEWAY"
[[ -f "$TOKENS" ]] || die "tokens file not found: $TOKENS (this script never creates or edits it)"
tok_mode="$(stat -c '%a' "$TOKENS")"
[[ "$tok_mode" == "600" || "$tok_mode" == "400" ]] || warn "tokens file mode is ${tok_mode} (expected 600); not changing it"

actual_sha="$(sha256sum "$GATEWAY" | cut -d' ' -f1)"
if [[ "$actual_sha" != "${EXPECTED_SHA}"* ]]; then
  if (( ALLOW_HASH_MISMATCH )); then
    warn "gateway.py sha256 ${actual_sha} does not start with ${EXPECTED_SHA} (continuing: --allow-hash-mismatch)"
  else
    die "gateway.py sha256 ${actual_sha} does not start with ${EXPECTED_SHA}; refusing (pass --allow-hash-mismatch if intended)"
  fi
fi
say "gateway.py sha256 ${actual_sha:0:16}... checked"

existing_pid="$(listener_pid)"
# Python processes of this user whose command line contains the gateway path
# (filters out this script itself when --gateway is passed).
manual_pids=""
for cand in $(pgrep -u "${EUID}" -f -- "$GATEWAY" || true); do
  [[ "$cand" == "$$" ]] && continue
  exe="$(readlink -f "/proc/${cand}/exe" 2>/dev/null || true)"
  [[ "$(basename "${exe:-x}")" == python* ]] || continue
  manual_pids+="${cand}"$'\n'
done

interpreter_of() {
  # The interpreter path the process was launched with, WITHOUT resolving
  # symlinks (a venv's bin/python3 must stay a venv path). argv[0] is looked up
  # in the process's own PATH when it is a bare name; /proc/<pid>/exe is the fallback.
  local pid="$1" argv0 cwd ppath dir
  argv0="$(tr '\0' '\n' < "/proc/${pid}/cmdline" | head -n1)"
  cwd="$(readlink "/proc/${pid}/cwd" 2>/dev/null || echo /)"
  if [[ "$argv0" == /* ]]; then echo "$argv0"; return; fi
  if [[ "$argv0" == */* ]]; then echo "${cwd}/${argv0}"; return; fi
  ppath="$(tr '\0' '\n' < "/proc/${pid}/environ" 2>/dev/null | sed -n 's/^PATH=//p' | head -n1)"
  IFS=':' read -r -a dirs <<< "${ppath:-/usr/local/bin:/usr/bin:/bin}"
  for dir in "${dirs[@]}"; do
    if [[ -n "$dir" && -x "${dir}/${argv0}" ]]; then echo "${dir}/${argv0}"; return; fi
  done
  readlink -f "/proc/${pid}/exe"
}

if [[ -z "$PYTHON" ]]; then
  first_manual="$(printf '%s\n' "$manual_pids" | head -n1)"
  if [[ -n "$first_manual" ]] && ! unit_owns_pid "$first_manual"; then
    PYTHON="$(interpreter_of "$first_manual")"
    say "Using the running gateway's interpreter: $PYTHON"
  else
    PYTHON="$(command -v python3 || true)"
  fi
fi
[[ -n "$PYTHON" && -x "$PYTHON" ]] || die "python interpreter not found (pass --python)"

expected_args="-B ${GATEWAY} --tokens ${TOKENS} --model ${MODEL}"
if [[ -n "$manual_pids" ]]; then
  while read -r mp; do
    [[ -n "$mp" ]] || continue
    unit_owns_pid "$mp" && continue
    cl="$(cmdline_of "$mp")"
    say "Manual gateway process: PID ${mp}: ${cl}"
    if [[ "$cl" != *"$expected_args"* ]]; then
      warn "its arguments differ from the unit's: '${expected_args}'"
      (( TAKEOVER )) && die "refusing --takeover: pass --gateway/--tokens/--model matching the running process"
    fi
    envnames="$(tr '\0' '\n' < "/proc/${mp}/environ" 2>/dev/null | cut -d= -f1 \
      | grep -E '^(OLLAMA|WISP|GX10|PYTHON|HTTPS?_PROXY|NO_PROXY|ALL_PROXY)' | tr '\n' ' ' || true)"
    [[ -n "$envnames" ]] && warn "manual process has env vars the unit will NOT inherit: ${envnames}(names only; add Environment= lines if the gateway needs them)"
  done <<< "$manual_pids"
fi

linger="$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------- render unit
say "Rendering ${UNIT_PATH}"
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
sed -e "s|@PYTHON3@|${PYTHON}|g" \
    -e "s|@GATEWAY_PY@|${GATEWAY}|g" \
    -e "s|@GATEWAY_DIR@|$(dirname "$GATEWAY")|g" \
    -e "s|@TOKENS_JSON@|${TOKENS}|g" \
    -e "s|@MODEL@|${MODEL}|g" \
    -e "s|@PORT@|${PORT}|g" \
    -e "s|@WAIT_SCRIPT@|${WAIT_SCRIPT}|g" \
    "${SCRIPT_DIR}/wisp-gx10-gateway.service" > "$rendered"
grep -q '@[A-Z0-9_]*@' "$rendered" && die "unrendered placeholder in unit"
if (( DRY_RUN )); then
  echo "----- ${UNIT_PATH} -----"; grep -v '^#' "$rendered" | sed '/^$/d'; echo "-----"
fi

unit_changed=1
if [[ -f "$UNIT_PATH" ]] && cmp -s "$rendered" "$UNIT_PATH"; then
  unit_changed=0
  say "Unit file unchanged"
fi

run mkdir -p "$UNIT_DIR" "$SUPPORT_DIR" "$STATE_DIR"
run chmod 700 "$STATE_DIR"
run install -m 0755 "${SCRIPT_DIR}/wait-for-ollama.sh" "$WAIT_SCRIPT"
if (( unit_changed )); then
  [[ -f "$UNIT_PATH" ]] && run cp -p "$UNIT_PATH" "${STATE_DIR}/${UNIT}.bak.${TS}"
  run install -m 0644 "$rendered" "$UNIT_PATH"
fi
if command -v systemd-analyze >/dev/null && (( ! DRY_RUN )); then
  systemd-analyze --user verify "$UNIT_PATH" 2>&1 | grep -v 'network-online.target' || true
fi
run systemctl --user daemon-reload
run systemctl --user enable "$UNIT"

# Record state for rollback (never secrets).
record_state() {
  local pid="$1"
  if (( DRY_RUN )); then echo "[dry-run] record rollback state for PID ${pid:-none} in ${STATE_DIR}"; return; fi
  {
    echo "installed_at=${TS}"
    echo "linger_before=${linger}"
    echo "python=${PYTHON}"
    echo "gateway=${GATEWAY}"
    echo "tokens=${TOKENS}"
    echo "model=${MODEL}"
    echo "port=${PORT}"
    if [[ -n "$pid" ]]; then
      echo "manual_pid=${pid}"
      echo "manual_cwd=$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || dirname "$GATEWAY")"
    fi
  } > "${STATE_DIR}/last-install.env"
  if [[ -n "$pid" && -r "/proc/${pid}/cmdline" ]]; then
    cp "/proc/${pid}/cmdline" "${STATE_DIR}/manual-cmdline.bin"
  fi
}

verify_unit() {
  local code
  for _ in $(seq 1 "$VERIFY_TIMEOUT"); do
    if systemctl --user is-active --quiet "$UNIT" && port_busy; then
      code="$(http_code)"
      if [[ "$code" =~ ^[234][0-9][0-9]$ ]]; then
        say "Gateway answering on 127.0.0.1:${PORT} (unauthenticated /v1/models -> HTTP ${code}; 401 is expected)"
        return 0
      fi
    fi
    sleep 1
  done
  return 1
}

relaunch_manual() {
  warn "Unit failed its health check; restoring the manual gateway"
  systemctl --user stop "$UNIT" || true
  systemctl --user disable "$UNIT" || true
  bash "${SCRIPT_DIR}/rollback-gateway-service.sh" --keep-unit-file || true
}

# ---------------------------------------------------------------- start / takeover
if [[ -n "$existing_pid" ]] && unit_owns_pid "$existing_pid"; then
  say "Gateway already supervised by ${UNIT} (PID ${existing_pid})"
  if (( RESTART || unit_changed )); then
    if (( unit_changed )); then say "Unit changed; restarting to apply"; fi
    run systemctl --user restart "$UNIT"
    (( DRY_RUN )) || verify_unit || die "unit did not come back healthy; see: journalctl --user -u ${UNIT} -n 50"
  fi
elif port_busy; then
  owner="${existing_pid:-unknown (not visible to $(id -un))}"
  if (( ! TAKEOVER )); then
    cat <<EOF

Something already listens on 127.0.0.1:${PORT} (PID ${owner}).
Refusing to start a duplicate gateway. The unit is installed and ENABLED, so
it will start at the next boot/login. To switch to supervision now (brief gap
of a few seconds; schedule it outside a demo):

  bash ${SCRIPT_DIR}/install-gateway-service.sh --takeover

or by hand:
  kill ${owner}                     # or Ctrl-C in the tmux/ssh window that runs it
  systemctl --user start ${UNIT}
EOF
    record_state "$existing_pid"
  else
    [[ -n "$existing_pid" ]] || die "cannot see the listener PID (different user?); stop it manually"
    cl="$(cmdline_of "$existing_pid")"
    [[ "$cl" == *"$GATEWAY"* ]] || die "listener PID ${existing_pid} is not ${GATEWAY} (${cl}); refusing to kill it"
    [[ "$(stat -c %U "/proc/${existing_pid}")" == "$(id -un)" ]] || die "listener is not owned by $(id -un)"
    record_state "$existing_pid"
    say "Takeover: stopping manual gateway PID ${existing_pid}"
    run kill -TERM "$existing_pid"
    if (( ! DRY_RUN )); then
      for _ in $(seq 1 20); do port_busy || break; sleep 0.5; done
      port_busy && die "port ${PORT} still busy 10 s after SIGTERM; not starting a duplicate. Investigate PID ${existing_pid}."
    fi
    run systemctl --user reset-failed "$UNIT" 2>/dev/null || true
    run systemctl --user start "$UNIT"
    if (( ! DRY_RUN )) && ! verify_unit; then
      relaunch_manual
      die "takeover failed and was rolled back; see: journalctl --user -u ${UNIT} -n 80"
    fi
  fi
else
  record_state ""
  if (( NO_START )); then
    say "--no-start: unit installed and enabled but not started"
  else
    say "Port ${PORT} is free; starting ${UNIT}"
    run systemctl --user reset-failed "$UNIT" 2>/dev/null || true
    run systemctl --user start "$UNIT"
    (( DRY_RUN )) || verify_unit || die "unit did not become healthy; see: journalctl --user -u ${UNIT} -n 80"
  fi
fi

# ---------------------------------------------------------------- linger
if [[ "$linger" == "yes" ]]; then
  say "Lingering is enabled for $(id -un): the unit starts at boot without a login"
elif (( ENABLE_LINGER )); then
  say "Enabling lingering for $(id -un)"
  if run loginctl enable-linger "$(id -un)"; then
    (( DRY_RUN )) || echo "linger_enabled_by_install=${TS}" >> "${STATE_DIR}/last-install.env"
  else
    warn "failed; run: sudo loginctl enable-linger $(id -un)"
  fi
else
  warn "Lingering is '${linger}': the user manager (and this unit) only starts after $(id -un) logs in."
  warn "To start at boot unattended run: loginctl enable-linger $(id -un)   (or with sudo)"
fi

print_rollback
say "Done"
