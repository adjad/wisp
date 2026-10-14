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
    # CGSession -suspend no longer exists on macOS 27; sleeping the display
    # locks the session under the default "require password after sleep".
    pmset displaysleepnow && echo "screen locked"
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
