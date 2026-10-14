#!/bin/bash
# Fixed Mac controls for the mac-controls skill.
#
# Every argument is checked against a closed set (or a 0-100 integer) before
# it reaches AppleScript. The skill runner already keeps each argument a single
# argv word, but an AppleScript source string is a second interpreter: a level
# of `50 & do shell script "…"` would be code if it were spliced in unchecked.
set -u

die() { echo "$1" >&2; exit 2; }

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
    # Claim the lock only once the session reports it; the key is present
    # only while the screen is locked.
    for _ in 1 2 3 4 5 6; do
      if ioreg -n Root -d1 | grep -q '"CGSSessionScreenIsLocked"=Yes'; then
        echo "screen locked"
        exit 0
      fi
      sleep 0.5
    done
    die "sent the Lock Screen shortcut, but macOS did not report the screen as locked"
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
