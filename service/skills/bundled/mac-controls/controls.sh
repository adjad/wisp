#!/bin/bash
# Fixed Mac controls for the mac-controls skill.
#
# Every argument is checked against a closed set (or a 0-100 integer) before
# it reaches AppleScript. The skill runner already keeps each argument a single
# argv word, but an AppleScript source string is a second interpreter: a level
# of `50 & do shell script "…"` would be code if it were spliced in unchecked.
set -u

die() { echo "$1" >&2; exit 2; }

# Whether THIS user's on-console session is locked, given `ioreg -n Root -d1 -a`
# output on stdin. IOConsoleUsers holds one entry per login session, and a
# background fast-user-switched session can be locked while the one on screen
# is not, so a match anywhere in the output proves nothing.
session_locked() {
  # Not $UID: bash 3.2 lets an inherited UID variable override it, which would
  # let the environment pick whose session vouches for the lock. `id` reads
  # the process's own credentials; if it cannot run, fail closed. Pipes, not
  # here-strings, so no temp file is needed.
  local plist uid i u on locked
  uid=$(/usr/bin/id -u 2>/dev/null) || return 1
  [[ "$uid" =~ ^[0-9]+$ ]] || return 1
  plist=$(cat) || return 1
  key() { printf '%s' "$plist" | plutil -extract "IOConsoleUsers.$i.$1" raw -o - - 2>/dev/null; }
  i=0
  while u=$(key kCGSSessionUserIDKey); do
    on=$(key kCGSSessionOnConsoleKey)
    if [[ "$u" == "$uid" && "$on" == "true" ]]; then
      locked=$(key CGSSessionScreenIsLocked)
      [[ "$locked" == "true" ]]
      return
    fi
    i=$((i + 1))
  done
  return 1
}

case "${1:-}" in
  get-volume)
    osascript -e 'set s to get volume settings' \
      -e 'return "volume " & (output volume of s) & "%, muted: " & (output muted of s)'
    ;;
  set-volume)
    level="${2:-}"
    [[ "$level" =~ ^[0-9]{1,3}$ ]] && (( 10#$level <= 100 )) \
      || die "level must be a whole number from 0 to 100"
    osascript -e "set volume output volume $((10#$level))" && echo "volume set to $((10#$level))%"
    ;;
  mute)
    case "${2:-}" in
      on) osascript -e 'set volume with output muted' && echo "muted" ;;
      off) osascript -e 'set volume without output muted' && echo "unmuted" ;;
      *) die "state must be on or off" ;;
    esac
    ;;
  lock)
    # Not `pmset displaysleepnow`: sleeping the display locks only when the
    # Mac asks for a password immediately after sleep, and this Mac's delay
    # was 300 seconds (sysadminctl -screenLock status) — "locked" would have
    # been false for five minutes. The Lock Screen shortcut locks at once.
    # CGSession -suspend, the old way, no longer exists on macOS 27.
    if ! err=$(osascript -e 'tell application "System Events" to keystroke "q" using {control down, command down}' 2>&1); then
      die "could not lock the screen: $err"
    fi
    # Claim the lock only once this user's on-console session reports it.
    for _ in 1 2 3 4 5 6; do
      if ioreg -n Root -d1 -a | session_locked; then
        echo "screen locked"
        exit 0
      fi
      sleep 0.5
    done
    die "sent the Lock Screen shortcut, but macOS did not report the screen as locked"
    ;;
  session-locked)
    # Read-only probe for tests: no skill tool's command template reaches it.
    session_locked && echo locked || echo unlocked
    ;;
  media)
    case "${2:-}" in
      play) verb="play" ;;
      pause) verb="pause" ;;
      next) verb="next track" ;;
      previous) verb="previous track" ;;
      *) die "action must be play, pause, next or previous" ;;
    esac
    case "${3:-music}" in
      music) app="Music" ;;
      spotify) app="Spotify" ;;
      *) die "app must be music or spotify" ;;
    esac
    osascript -e "tell application \"$app\" to $verb" && echo "$app: ${2}"
    ;;
  *)
    die "unknown control: ${1:-}"
    ;;
esac
