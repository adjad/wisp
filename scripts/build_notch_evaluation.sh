#!/bin/bash
# Build and launch the contained Notch evaluation host: a separately marked
# development binary of the real notch Ask surface, plus the guarded backend.
#
#   scripts/build_notch_evaluation.sh build  <out_dir> [--sign IDENTITY]
#   scripts/build_notch_evaluation.sh launch <out_dir> <owned_root> [--inference-url URL --model ID ...]
#        [--snapshot DIR] [--live-sources] [--bundle APP]
#   scripts/build_notch_evaluation.sh stop   <owned_root>
#
# Never installs, signs as, replaces or opens Wisp.app; never uses port 8765,
# 8000 or 8001; never reads or writes ~/.moe. `--sign` signs only this
# bundle, under its own identifier, so its permissions are its own and never
# Wisp.app's. `--snapshot` installs a verified snapshot and binds its manifest
# digest; `--live-sources` lets the app's own readers push read-only reads. Both directories must be fresh
# scratch under the system temporary area; they are validated before anything
# is created (scripts/notch_evaluation/lifecycle.py preflight). `launch` hands
# both processes to an owned-child supervisor; `stop` asks that supervisor to
# retire them and never signals a pid read from a file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
APP_NAME="Wisp Notch Evaluation (DEV)"
BUNDLE_ID="com.wisp.notch-evaluation.dev"
PYTHON="${WISP_EVALUATION_PYTHON:-$ROOT/.venv/bin/python}"
LISTEN_PORT="${WISP_EVALUATION_PORT:-18891}"
LIFECYCLE=("$PYTHON" -I -B "$ROOT/scripts/notch_evaluation/lifecycle.py")

die() { echo "build_notch_evaluation: $*" >&2; exit 1; }

cmd_build() {
  local out_arg="${1:?out_dir}" identity=""; shift
  while (($#)); do
    case "$1" in
      --sign) identity="$2"; shift 2 ;;
      *) die "unknown option $1" ;;
    esac
  done
  local out
  out="$("${LIFECYCLE[@]}" preflight --create "$out_arg")" || die "refusing the build directory"
  /usr/bin/swift build --disable-sandbox --package-path "$ROOT/app" --scratch-path "$out/swift-build" \
    -Xswiftc -DWISP_NOTCH_EVALUATION -q
  local app="$out/$APP_NAME.app"
  mkdir -p "$app/Contents/MacOS"
  cp "$out/swift-build/debug/WispApp" "$app/Contents/MacOS/WispNotchEvaluation"
  /usr/bin/plutil -create xml1 "$app/Contents/Info.plist"
  /usr/bin/plutil -insert CFBundleIdentifier -string "$BUNDLE_ID" "$app/Contents/Info.plist"
  /usr/bin/plutil -insert CFBundleName -string "$APP_NAME" "$app/Contents/Info.plist"
  /usr/bin/plutil -insert CFBundleExecutable -string WispNotchEvaluation "$app/Contents/Info.plist"
  /usr/bin/plutil -insert CFBundlePackageType -string APPL "$app/Contents/Info.plist"
  /usr/bin/plutil -insert NSPrincipalClass -string NSApplication "$app/Contents/Info.plist"
  /usr/bin/plutil -insert WispEvaluationBuild -string "contained development host; not Wisp.app" "$app/Contents/Info.plist"
  local purpose="Wisp Notch Evaluation (DEV) reads this, read-only, to answer evaluation prompts. It never changes it."
  local key
  for key in NSCalendarsUsageDescription NSCalendarsFullAccessUsageDescription NSRemindersUsageDescription \
             NSRemindersFullAccessUsageDescription NSContactsUsageDescription NSAppleEventsUsageDescription; do
    /usr/bin/plutil -insert "$key" -string "$purpose" "$app/Contents/Info.plist"
  done
  if [[ -n "$identity" ]]; then
    /usr/bin/codesign --force --sign "$identity" --identifier "$BUNDLE_ID" "$app"
    /usr/bin/codesign --verify --strict "$app"
  fi
  /usr/bin/shasum -a 256 "$app/Contents/MacOS/WispNotchEvaluation" | tee "$out/binary.sha256"
  echo "built: $app"
}

# The app's OS profile. AppKit and WindowServer work; AppleEvents, Launch
# Services opens, notifications and the pasteboard are refused; the network is
# the evaluation backend only; files under the home are readable only in the
# owned root, its source roots and the app bundle; writes go only to the owned
# root (TMPDIR is inside it) and this bundle's own caches. Native QA qualifies
# this profile before any corpus use.
#
# A live-source run (the user approved it and grants this bundle's own
# permissions) differs in exactly these ways: AppleEvents may go to Mail and
# Notes only, for the readers' fixed read scripts; the AppleEvents and Launch
# Services lookups those need are not refused; Mail, Messages and the Notes
# store are readable (never writable); and this bundle's own preferences file
# is writable. The app applies the profile to itself (see
# NotchEvaluationSelfSandbox), because it is launched through Launch Services.
app_profile() {
  local owned="$1" image="$2" bundle="$3" live="${4:-}" home cache
  home="$(/usr/bin/dscl . -read "/Users/$(/usr/bin/id -un)" NFSHomeDirectory | /usr/bin/awk '{print $2}')"
  cache="$(/usr/bin/getconf DARWIN_USER_CACHE_DIR)"
  cache="$(cd "$cache" && pwd -P)"
  local lookups='(global-name "com.apple.coreservices.appleevents")
  (global-name "com.apple.coreservices.launchservicesd") (global-name "com.apple.lsd.mapdb")
  ' reads="" prefs="" events=""
  if [[ -n "$live" ]]; then
    lookups=""
    reads="(subpath \"$home/Library/Mail\") (subpath \"$home/Library/Messages\")
    (subpath \"$home/Library/Group Containers/group.com.apple.notes\")"
    prefs="(literal \"$home/Library/Preferences/$BUNDLE_ID.plist\")"
    events='(allow appleevent-send (appleevent-destination "com.apple.mail") (appleevent-destination "com.apple.Notes"))'
  fi
  cat <<PROFILE
(version 1)
(allow default)
(deny appleevent-send)
$events
(deny process-exec (require-not (literal "$image")))
(deny process-fork)
(deny mach-lookup $lookups(global-name "com.apple.usernoted.client") (global-name "com.apple.pasteboard.1")
  (global-name "com.apple.coreservices.quarantine-resolver"))
(deny network*)
(allow network-outbound (remote ip "localhost:$LISTEN_PORT"))
(deny file-read-data (require-all (subpath "$home")
  (require-not (require-any (subpath "$owned") (subpath "$bundle") $reads))))
(deny file-write* (require-not (require-any (subpath "$owned") (literal "/dev/null")
  (subpath "$home/Library/Caches/$BUNDLE_ID")
  (subpath "$home/Library/Saved Application State/$BUNDLE_ID.savedState")
  (subpath "$cache$BUNDLE_ID") $prefs)))
PROFILE
}

cmd_launch() {
  local out_arg="${1:?out_dir}" owned_arg="${2:?owned_root}"; shift 2
  local inference="" models=() snapshot="" live="" bundle=""
  while (($#)); do
    case "$1" in
      --inference-url) inference="$2"; shift 2 ;;
      --model) models+=("$2"); shift 2 ;;
      --snapshot) snapshot="$2"; shift 2 ;;
      --live-sources) live=1; shift ;;
      --bundle) bundle="$2"; shift 2 ;;
      *) die "unknown option $1" ;;
    esac
  done
  local out owned
  out="$(cd "$out_arg" && pwd -P)" || die "no build directory"
  local bundle_dir="$out/$APP_NAME.app"
  if [[ -n "$bundle" ]]; then
    bundle_dir="$(cd "$bundle" && pwd -P)" || die "no bundle at $bundle"
    [[ "$(/usr/bin/plutil -extract CFBundleIdentifier raw "$bundle_dir/Contents/Info.plist")" == "$BUNDLE_ID" ]] \
      || die "$bundle is not the evaluation bundle"
  fi
  local app="$bundle_dir/Contents/MacOS/WispNotchEvaluation"
  [[ -x "$app" && ! -L "$app" ]] || die "build first: $app is missing"
  # Validated as a fresh scratch root BEFORE it is created or written.
  owned="$("${LIFECYCLE[@]}" preflight --create "$owned_arg")" || die "refusing the owned root"
  local manifest=""
  if [[ -n "$snapshot" ]]; then
    manifest="$("${LIFECYCLE[@]}" install-snapshot "$snapshot" "$owned")" || die "refusing the snapshot"
  fi
  local run_id token nonce
  run_id="notch-$(/bin/date +%Y%m%d%H%M%S)"
  token="$("${LIFECYCLE[@]}" secret --bytes 32)"
  nonce="$("${LIFECYCLE[@]}" secret --bytes 16)"
  local models_json="[]"
  if ((${#models[@]})); then models_json="$(printf '"%s",' "${models[@]}")"; models_json="[${models_json%,}]"; fi
  (umask 077; set -o noclobber
   printf '{"run_id":"%s","owned_root":"%s","listen_port":%s,"token":"%s","nonce":"%s","source_roots":["%s/home"],"allowed_models":%s%s%s%s}\n' \
     "$run_id" "$owned" "$LISTEN_PORT" "$token" "$nonce" "$owned" "$models_json" \
     "${inference:+,\"inference_url\":\"$inference\"}" \
     "${manifest:+,\"snapshot_manifest_sha256\":\"$manifest\"}" \
     "${live:+,\"source_mode\":\"live\"}" > "$owned/host-config.json"
   "$PYTHON" -I -B "$ROOT/scripts/notch_evaluation/host.py" --config "$owned/host-config.json" \
     --print-profile "$PYTHON" > "$owned/host.sb"
   app_profile "$owned" "$app" "$bundle_dir" "$live" > "$owned/app.sb")
  # The supervisor owns both children for the life of the run; it starts the
  # backend in the owned root with a clean environment, checks its identity,
  # then starts the app with TMPDIR inside the owned root.
  (cd "$owned" && /usr/bin/nohup "${LIFECYCLE[@]}" supervise "$owned" --python "$PYTHON" \
     --host "$ROOT/scripts/notch_evaluation/host.py" --app "$app" ${live:+--bundle "$bundle_dir"} \
     > /dev/null 2>&1 &)
  local tries=0 state=""
  while ((tries < 400)); do
    state="$(/usr/bin/plutil -extract state raw "$owned/lifecycle.json" 2>/dev/null || true)"
    [[ "$state" == running || "$state" == failed || "$state" == ended ]] && break
    tries=$((tries + 1)); /bin/sleep 0.1
  done
  /bin/cat "$owned/lifecycle.json" 2>/dev/null || true
  [[ "$state" == running ]] || die "launch did not reach running (state: ${state:-unknown}); see $owned"
}

cmd_stop() {
  "${LIFECYCLE[@]}" stop "${1:?owned_root}"
}

case "${1:-}" in
  build) shift; cmd_build "$@" ;;
  launch) shift; cmd_launch "$@" ;;
  stop) shift; cmd_stop "$@" ;;
  *) die "usage: build <out_dir> [--sign ID] | launch <out_dir> <owned_root> [--inference-url URL --model ID] | stop <owned_root>" ;;
esac
