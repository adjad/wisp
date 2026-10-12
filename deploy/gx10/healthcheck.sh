#!/usr/bin/env bash
# Read-only health check of the GX10 inference stack. Run on the GX10 as asus.
# Changes nothing and never reads or prints tokens.
#
#   bash deploy/gx10/healthcheck.sh
#
# Checks: Ollama /api/version + loaded models, gateway listener on :8767 and
# whether systemd supervises it, unauthenticated gateway answer (401 expected),
# lingering, Tailscale Serve config, optional brain server on :8790.
set -euo pipefail

GW_PORT="${GW_PORT:-8767}"
BRAIN_PORT="${BRAIN_PORT:-8790}"
OLLAMA="${OLLAMA:-http://127.0.0.1:11434}"
fail=0

ok()   { printf '  [ok]   %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; fail=1; }
info() { printf '  [info] %s\n' "$*"; }

if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/${EUID}" ]]; then
  XDG_RUNTIME_DIR="/run/user/${EUID}"
  export XDG_RUNTIME_DIR
fi

echo "== Ollama"
if ver="$(curl -fsS --max-time 3 "${OLLAMA}/api/version" 2>/dev/null)"; then
  ok "Ollama answering: ${ver}"
else
  bad "Ollama not answering at ${OLLAMA} (sudo systemctl status ollama)"
fi
if command -v ollama >/dev/null 2>&1; then
  ollama ps 2>/dev/null | sed 's/^/         /' || true
fi
if systemctl is-active --quiet ollama 2>/dev/null; then ok "ollama.service active"; else info "ollama.service not active or not a system unit"; fi

echo "== Gateway (127.0.0.1:${GW_PORT})"
listen="$(ss -Hltnp "sport = :${GW_PORT}" 2>/dev/null || true)"
if [[ -n "$listen" ]]; then
  pid="$(grep -o 'pid=[0-9]*' <<<"$listen" | head -n1 | cut -d= -f2 || true)"
  ok "listener present${pid:+ (PID ${pid})}"
  [[ -n "$pid" ]] && info "cmd: $(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null | sed -E 's/(--tokens )[^ ]+/\1<path>/' || true)"
else
  bad "nothing listens on 127.0.0.1:${GW_PORT} -> Send on the Mac will fail"
fi
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${GW_PORT}/v1/models" || true)"
case "$code" in
  401|403) ok "unauthenticated /v1/models -> ${code} (auth enforced, gateway alive)" ;;
  200) info "unauthenticated /v1/models -> 200 (unexpected: is auth on?)" ;;
  *) bad "gateway HTTP probe -> ${code}" ;;
esac
if systemctl --user cat wisp-gx10-gateway.service >/dev/null 2>&1; then
  state="$(systemctl --user is-active wisp-gx10-gateway.service 2>/dev/null || true)"
  enabled="$(systemctl --user is-enabled wisp-gx10-gateway.service 2>/dev/null || true)"
  info "wisp-gx10-gateway.service: ${state}, ${enabled}"
  main="$(systemctl --user show -p MainPID --value wisp-gx10-gateway.service 2>/dev/null || echo 0)"
  if [[ -n "${pid:-}" && "$main" == "$pid" ]]; then ok "gateway is supervised by systemd"; else info "gateway is NOT the unit's process (manual launch?)"; fi
else
  info "wisp-gx10-gateway.service not installed (gateway will not restart after reboot)"
fi
linger="$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null || echo unknown)"
if [[ "$linger" == yes ]]; then ok "linger=yes (user units start at boot)"; else info "linger=${linger} (user units start only after login)"; fi

echo "== Tailscale Serve"
if command -v tailscale >/dev/null 2>&1; then
  tailscale serve status 2>/dev/null | sed 's/^/         /' || info "tailscale serve status failed (needs sudo?)"
else
  info "tailscale CLI not found"
fi

echo "== Brain server (optional, 127.0.0.1:${BRAIN_PORT})"
bcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${BRAIN_PORT}/healthz" || true)"
case "$bcode" in
  200) ok "brain /healthz 200" ;;
  503) bad "brain up but engine unavailable (journalctl --user -u wisp-brain -n 50)" ;;
  000) info "brain server not running (fine unless brain.mode remote)" ;;
  *) bad "brain /healthz -> ${bcode}" ;;
esac

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "== GPU"
  nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null | sed 's/^/         /' || true
fi

echo
if (( fail )); then echo "RESULT: problems found"; exit 1; fi
echo "RESULT: healthy"
