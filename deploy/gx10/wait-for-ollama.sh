#!/usr/bin/env bash
# Readiness helper for the GX10 units (used as ExecStartPre=).
#   wait-for-ollama.sh --url http://127.0.0.1:11434/api/version --timeout 180
#       exit 0 once the URL answers HTTP 2xx, 1 after the timeout.
#   wait-for-ollama.sh --require-free-port 8767
#       exit 0 if nothing listens on 127.0.0.1:<port>, 1 otherwise
#       (prevents a duplicate gateway next to a manually started one).
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

url="http://127.0.0.1:11434/api/version"
timeout_s=180
port=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) url="$2"; shift 2 ;;
    --timeout) timeout_s="$2"; shift 2 ;;
    --require-free-port) port="$2"; shift 2 ;;
    -h|--help) print_header "$0"; exit 0 ;;
    *) echo "wait-for-ollama: unknown argument $1" >&2; exit 2 ;;
  esac
done

port_listening() {
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    [[ -n "$(ss -Hltn "sport = :${p}" 2>/dev/null)" ]]
  else
    python3 - "$p" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
  fi
}

probe() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 3 -o /dev/null "$url"
  else
    python3 - "$url" <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as r:
        sys.exit(0 if 200 <= r.status < 300 else 1)
except Exception:
    sys.exit(1)
PY
  fi
}

if [[ -n "$port" ]]; then
  if port_listening "$port"; then
    echo "wait-for-ollama: 127.0.0.1:${port} is already in use (manual gateway still running?). Refusing to start a duplicate." >&2
    exit 1
  fi
  exit 0
fi

deadline=$((SECONDS + timeout_s))
until probe; do
  if (( SECONDS >= deadline )); then
    echo "wait-for-ollama: ${url} not ready after ${timeout_s}s" >&2
    exit 1
  fi
  sleep 2
done
echo "wait-for-ollama: ${url} ready"
