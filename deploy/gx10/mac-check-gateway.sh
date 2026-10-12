#!/usr/bin/env bash
# Read-only check of the GX10 gateway (and optionally the brain server) FROM THE
# INTEL MAC, using the token Wisp already stores in the login Keychain. The
# token is never printed and never placed on a command line (curl reads the
# header from a 0600 temp file that is deleted on exit).
#
#   bash deploy/gx10/mac-check-gateway.sh              # /v1/models + 1 tiny chat
#   bash deploy/gx10/mac-check-gateway.sh --no-chat    # skip the generation test
#   bash deploy/gx10/mac-check-gateway.sh --brain https://6wjp.tail4cc204.ts.net:8443
#
# Keychain item used by Wisp (service/config/provider_credentials.py):
#   service com.wisp.inference, account "gx10demo:<sha256 of the base URL>"
# Brain token (service/assistant/behavior_runtime.py): service com.wisp.inference,
#   account "wispbrain:<sha256 of WISP_BRAIN_URL exactly as configured, no trailing slash>"
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

BASE="https://6wjp.tail4cc204.ts.net"
MODEL="qwen3.6:35b"
CHAT=1
BRAIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) BASE="${2%/}"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --no-chat) CHAT=0; shift ;;
    --brain) BRAIN="${2%/}"; shift 2 ;;
    -h|--help) print_header "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

fail=0
ok()  { printf '  [ok]   %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; fail=1; }

sha() { printf '%s' "$1" | shasum -a 256 | awk '{print $1}'; }
HDR="$(mktemp)"
BODY="$(mktemp)"
chmod 600 "$HDR" "$BODY"
trap 'rm -f "$HDR" "$BODY"' EXIT

write_auth_header() {  # service account -> writes header file; returns 1 if missing
  local tok
  tok="$(/usr/bin/security find-generic-password -s "$1" -a "$2" -w 2>/dev/null)" || return 1
  [[ -n "$tok" ]] || return 1
  printf 'Authorization: Bearer %s\n' "$tok" > "$HDR"
}

echo "== Gateway ${BASE}"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "${BASE}/v1/models" || true)"
case "$code" in
  401|403) ok "reachable over Tailscale; unauthenticated -> ${code} (auth enforced)" ;;
  000) bad "unreachable (Tailscale connected? try: tailscale ping 6wjp)" ;;
  502|503|504) bad "HTTP ${code}: Tailscale Serve is up but the gateway behind it is down (restart it on the GX10)" ;;
  *) bad "unexpected HTTP ${code} without a token" ;;
esac

if write_auth_header com.wisp.inference "gx10demo:$(sha "$BASE")"; then
  ok "gateway token found in Keychain (not shown)"
  code="$(curl -s -o "$BODY" -w '%{http_code}' --max-time 10 -H @"$HDR" "${BASE}/v1/models" || true)"
  if [[ "$code" == 200 ]] && grep -q "\"${MODEL}\"" "$BODY"; then
    ok "/v1/models -> 200 and lists ${MODEL}"
  else
    bad "/v1/models -> HTTP ${code}$( [[ "$code" == 200 ]] && echo " but ${MODEL} not listed")"
  fi
  if (( CHAT )); then
    printf '{"model":"%s","messages":[{"role":"user","content":"Reply with the single word OK. /no_think"}],"max_tokens":16,"stream":false,"temperature":0}' "$MODEL" > "${BODY}.req"
    out="$(curl -s -o "$BODY" -w '%{http_code} %{time_total}' --max-time 60 -H @"$HDR" \
      -H 'Content-Type: application/json' --data-binary @"${BODY}.req" "${BASE}/v1/chat/completions" || true)"
    rm -f "${BODY}.req"
    if [[ "${out%% *}" == 200 ]]; then ok "chat completion -> 200 in ${out##* }s"; else bad "chat completion -> ${out}"; fi
  fi
else
  bad "no Keychain item com.wisp.inference / gx10demo:<sha256(${BASE})> (is this the Intel demo Mac?)"
fi

if [[ -n "$BRAIN" ]]; then
  echo "== Brain ${BRAIN}"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "${BRAIN}/healthz" || true)"
  if [[ "$code" == 200 ]]; then ok "/healthz 200"; else bad "/healthz -> ${code}"; fi
  : > "$HDR"
  if write_auth_header com.wisp.inference "wispbrain:$(sha "$BRAIN")"; then
    code="$(curl -s -o "$BODY" -w '%{http_code}' --max-time 8 -H @"$HDR" "${BRAIN}/v1/status" || true)"
    if [[ "$code" == 200 ]]; then ok "/v1/status 200"; else bad "/v1/status -> ${code}"; fi
  else
    bad "no Keychain item com.wisp.inference / wispbrain:<sha256(${BRAIN})> (see RUNBOOK: Brain token to the Mac)"
  fi
fi

echo
if (( fail )); then echo "RESULT: problems found"; exit 1; fi
echo "RESULT: healthy"
