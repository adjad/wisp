#!/bin/bash
# Build and launch the contained Notch evaluation host: a separately marked
# development binary of the real notch Ask surface, plus the guarded backend.
#
#   scripts/build_notch_evaluation.sh build  <out_dir>
#   scripts/build_notch_evaluation.sh launch <out_dir> <owned_root> [--inference-url URL --model ID ...]
#   scripts/build_notch_evaluation.sh stop   <owned_root>
#
# Never installs, signs as, replaces or opens Wisp.app; never uses port 8765,
# 8000 or 8001; never reads or writes ~/.moe. `launch` runs the backend and the
# app each under its own sandbox-exec profile and records both pids in the
# owned root, which `stop` uses to end exactly those processes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
APP_NAME="Wisp Notch Evaluation (DEV)"
BUNDLE_ID="com.wisp.notch-evaluation.dev"
PYTHON="${WISP_EVALUATION_PYTHON:-$ROOT/.venv/bin/python}"
LISTEN_PORT="${WISP_EVALUATION_PORT:-18891}"

die() { echo "build_notch_evaluation: $*" >&2; exit 1; }

refuse_installed_paths() {
  local path
  path="$(cd "$1" 2>/dev/null && pwd -P || echo "$1")"
  case "$path" in
    /Applications*|"$HOME/Applications"*|"$HOME/.moe"*) die "refusing $path: outside a development scratch location" ;;
  esac
}

cmd_build() {
  local out="${1:?out_dir}"
  mkdir -p "$out"
  refuse_installed_paths "$out"
  out="$(cd "$out" && pwd -P)"
  /usr/bin/swift build --disable-sandbox --package-path "$ROOT/app" --scratch-path "$out/swift-build" \
    -Xswiftc -DWISP_NOTCH_EVALUATION -q
  local app="$out/$APP_NAME.app"
  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS"
  cp "$out/swift-build/debug/WispApp" "$app/Contents/MacOS/WispNotchEvaluation"
  /usr/bin/plutil -create xml1 "$app/Contents/Info.plist"
  /usr/bin/plutil -insert CFBundleIdentifier -string "$BUNDLE_ID" "$app/Contents/Info.plist"
  /usr/bin/plutil -insert CFBundleName -string "$APP_NAME" "$app/Contents/Info.plist"
  /usr/bin/plutil -insert CFBundleExecutable -string WispNotchEvaluation "$app/Contents/Info.plist"
  /usr/bin/plutil -insert CFBundlePackageType -string APPL "$app/Contents/Info.plist"
  /usr/bin/plutil -insert NSPrincipalClass -string NSApplication "$app/Contents/Info.plist"
  /usr/bin/plutil -insert WispEvaluationBuild -string "contained development host; not Wisp.app" "$app/Contents/Info.plist"
  /usr/bin/shasum -a 256 "$app/Contents/MacOS/WispNotchEvaluation" | tee "$out/binary.sha256"
  echo "built: $app"
}

# The app's OS profile: AppKit and WindowServer work, but no AppleEvents,
# Launch Services opens, notifications or pasteboard; network only to the
# evaluation backend; writes only to the owned root and the app's own caches.
app_profile() {
  local owned="$1" home
  home="$(/usr/bin/dscl . -read "/Users/$(/usr/bin/id -un)" NFSHomeDirectory | /usr/bin/awk '{print $2}')"
  cat <<PROFILE
(version 1)
(allow default)
(deny appleevent-send)
(deny process-exec (require-not (literal "$2")))
(deny process-fork)
(deny mach-lookup (global-name "com.apple.coreservices.appleevents")
  (global-name "com.apple.usernoted.client") (global-name "com.apple.pasteboard.1")
  (global-name "com.apple.coreservices.quarantine-resolver"))
(deny network*)
(allow network-outbound (remote ip "localhost:$LISTEN_PORT"))
(deny file-write* (require-not (require-any (subpath "$owned") (literal "/dev/null")
  (subpath "$home/Library/Caches/$BUNDLE_ID")
  (subpath "$home/Library/Saved Application State/$BUNDLE_ID.savedState")
  (regex #"^/private/var/folders/"))))
PROFILE
}

cmd_launch() {
  local out="${1:?out_dir}" owned="${2:?owned_root}"; shift 2
  local inference="" models=()
  while (($#)); do
    case "$1" in
      --inference-url) inference="$2"; shift 2 ;;
      --model) models+=("$2"); shift 2 ;;
      *) die "unknown option $1" ;;
    esac
  done
  out="$(cd "$out" && pwd -P)"
  local app="$out/$APP_NAME.app/Contents/MacOS/WispNotchEvaluation"
  [[ -x "$app" ]] || die "build first: $app is missing"
  mkdir -p "$owned"; chmod 700 "$owned"
  owned="$(cd "$owned" && pwd -P)"
  refuse_installed_paths "$owned"
  [[ ! -e "$owned/host.pid" ]] || die "$owned already has a running or unstopped host"
  local run_id token nonce
  run_id="notch-$(/bin/date +%Y%m%d%H%M%S)"
  token="$(/usr/bin/openssl rand -hex 32)"
  nonce="$(/usr/bin/openssl rand -hex 16)"
  local models_json="[]"
  if ((${#models[@]})); then models_json="$(printf '"%s",' "${models[@]}")"; models_json="[${models_json%,}]"; fi
  (umask 077
   printf '{"run_id":"%s","owned_root":"%s","listen_port":%s,"token":"%s","nonce":"%s","source_roots":["%s/home"],"allowed_models":%s%s}\n' \
     "$run_id" "$owned" "$LISTEN_PORT" "$token" "$nonce" "$owned" "$models_json" \
     "${inference:+,\"inference_url\":\"$inference\"}" > "$owned/host-config.json")
  "$PYTHON" -I -B "$ROOT/scripts/notch_evaluation/host.py" --config "$owned/host-config.json" \
    --print-profile "$PYTHON" > "$owned/host.sb"
  # A clean environment: nothing inherited reaches the backend, which starts
  # in the owned root (the protected home is not even a working directory).
  cd "$owned"
  /usr/bin/env -i LANG=C.UTF-8 PYTHONHASHSEED=0 /usr/bin/sandbox-exec -f "$owned/host.sb" \
    "$PYTHON" -I -B "$ROOT/scripts/notch_evaluation/host.py" --config "$owned/host-config.json" \
    > "$owned/host.log" 2>&1 &
  local backend=$!
  echo "$backend" > "$owned/host.pid"
  local tries=0
  until /usr/bin/curl -sf -H "x-wisp-evaluation-token: $token" "http://127.0.0.1:$LISTEN_PORT/evaluation/identity" \
      -o "$owned/identity.json" 2>/dev/null; do
    tries=$((tries + 1)); ((tries < 100)) || { kill "$backend" 2>/dev/null; die "backend did not answer; see $owned/host.log"; }
    kill -0 "$backend" 2>/dev/null || die "backend exited; see $owned/host.log"
    /bin/sleep 0.1
  done
  grep -q "\"nonce\": \"$nonce\"" "$owned/identity.json" || { kill "$backend"; die "identity mismatch"; }
  (umask 077
   printf '{"run_id":"%s","owned_root":"%s","listen_port":%s,"token":"%s","nonce":"%s","backend_pid":%s}\n' \
     "$run_id" "$owned" "$LISTEN_PORT" "$token" "$nonce" "$backend" > "$owned/native-config.json")
  app_profile "$owned" "$(cd "$(dirname "$app")" && pwd -P)/$(basename "$app")" > "$owned/app.sb"
  /usr/bin/env -i HOME="$HOME" LANG=C.UTF-8 WISP_NOTCH_EVALUATION_CONFIG="$owned/native-config.json" \
    /usr/bin/sandbox-exec -f "$owned/app.sb" "$app" > "$owned/app.log" 2>&1 &
  echo $! > "$owned/app.pid"
  echo "run $run_id: backend pid $backend, app pid $(cat "$owned/app.pid"), evidence in $owned/evidence"
}

cmd_stop() {
  local owned="${1:?owned_root}"
  for name in app host; do
    if [[ -f "$owned/$name.pid" ]]; then
      local pid; pid="$(cat "$owned/$name.pid")"
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 50); do kill -0 "$pid" 2>/dev/null || break; /bin/sleep 0.1; done
      kill -9 "$pid" 2>/dev/null || true
      rm -f "$owned/$name.pid"
    fi
  done
  echo "stopped"
}

case "${1:-}" in
  build) shift; cmd_build "$@" ;;
  launch) shift; cmd_launch "$@" ;;
  stop) shift; cmd_stop "$@" ;;
  *) die "usage: build <out_dir> | launch <out_dir> <owned_root> [--inference-url URL --model ID] | stop <owned_root>" ;;
esac
