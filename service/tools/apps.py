"""App-control tools: open / quit apps and drive Spotify & Music.

All carry the `app_control` safety category, so the policy engine gates them:
in view-only mode they're denied; otherwise they require the user's confirmation
(see service/safety/policy.py). Registered on import.
"""
from __future__ import annotations

import subprocess

from service.tools.registry import register


def _osascript(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=20)


def _as_str(value: str) -> str:
    """Quote `value` as an AppleScript string literal.

    These scripts are passed to osascript as a single argv element — no shell is
    involved — so shell quoting is the wrong tool for the job, and it was
    previously used here for all three interpolations below. shlex.quote only
    adds quotes when a string contains shell-special characters, and it uses
    SINGLE quotes, which AppleScript does not accept for strings at all. So the
    bug went both ways:

      quit_app("Calculator")     -> tell application Calculator to quit
                                    (-2753 "The variable Calculator is not defined")
      quit_app("Google Chrome")  -> tell application 'Google Chrome' to quit
                                    (single quotes are a syntax error)

    i.e. quit_app failed for EVERY app name, one-word or not, and the Spotify
    URI / Music playlist interpolations had the same defect. AppleScript string
    literals are double-quoted with backslash escapes, which is what this
    produces.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=20)


@register(
    "open_app",
    "Open (launch or bring to front) a macOS application by name, e.g. 'Spotify', "
    "'Safari', 'Notes'. Optionally open a file or URL with it.",
    {"type": "object",
     "properties": {
         "name": {"type": "string", "description": "application name, e.g. 'Spotify'"},
         "target": {"type": "string",
                    "description": "optional file path or URL to open with the app"},
     },
     "required": ["name"]},
    category="app_control",
)
def open_app(name: str, target: str | None = None) -> str:
    argv = ["open", "-a", name]
    if target:
        argv.append(target)
    p = _run(argv)
    if p.returncode != 0:
        return f"(could not open {name!r}: {p.stderr.strip() or 'unknown error'})"
    return f"opened {name}" + (f" with {target}" if target else "")


@register(
    "quit_app",
    "Quit a macOS application by name. Asks the app to quit gracefully.",
    {"type": "object",
     "properties": {"name": {"type": "string", "description": "application name"}},
     "required": ["name"]},
    category="app_control",
)
def quit_app(name: str) -> str:
    p = _osascript(f'tell application {_as_str(name)} to quit')
    if p.returncode != 0:
        return f"(could not quit {name!r}: {p.stderr.strip() or 'unknown error'})"
    return f"asked {name} to quit"


@register(
    "spotify",
    "Control the Spotify desktop app: play a playlist/track/album by its Spotify URI, "
    "or run a transport command (play, pause, next, previous). Spotify must be "
    "installed; it will be launched if not already running.",
    {"type": "object",
     "properties": {
         "action": {"type": "string",
                    "enum": ["play_uri", "play", "pause", "next", "previous"],
                    "description": "what to do"},
         "uri": {"type": "string",
                 "description": "Spotify URI for action 'play_uri', e.g. "
                                "'spotify:playlist:37i9dQZF1DXcBWIGoYBM5M'"},
     },
     "required": ["action"]},
    category="app_control",
)
def spotify(action: str, uri: str | None = None) -> str:
    verbs = {"play": "play", "pause": "pause",
             "next": "next track", "previous": "previous track"}
    if action == "play_uri":
        if not uri:
            return "(spotify play_uri needs a 'uri', e.g. spotify:playlist:...)"
        script = f'tell application "Spotify" to play track {_as_str(uri)}'
    elif action in verbs:
        script = f'tell application "Spotify" to {verbs[action]}'
    else:
        return f"(unknown spotify action: {action})"
    p = _osascript(script)
    if p.returncode != 0:
        return f"(spotify error: {p.stderr.strip() or 'is Spotify installed?'})"
    return f"spotify: {action}" + (f" {uri}" if uri else "")


@register(
    "music",
    "Control the Apple Music desktop app (ships with macOS): play a playlist "
    "by name, or run a transport command (play, pause, next, previous). Use "
    "this instead of `spotify` when the user says 'Music' / 'Apple Music' or "
    "doesn't have Spotify.",
    {"type": "object",
     "properties": {
         "action": {"type": "string",
                    "enum": ["play_playlist", "play", "pause", "next", "previous"],
                    "description": "what to do"},
         "playlist": {"type": "string",
                      "description": "playlist name for action 'play_playlist'"},
     },
     "required": ["action"]},
    category="app_control",
)
def music(action: str, playlist: str | None = None) -> str:
    verbs = {"play": "play", "pause": "pause",
             "next": "next track", "previous": "previous track"}
    if action == "play_playlist":
        if not playlist:
            return "(music play_playlist needs a 'playlist' name)"
        script = f'tell application "Music" to play playlist {_as_str(playlist)}'
    elif action in verbs:
        script = f'tell application "Music" to {verbs[action]}'
    else:
        return f"(unknown music action: {action})"
    p = _osascript(script)
    if p.returncode != 0:
        return f"(music error: {p.stderr.strip() or 'is Music installed?'})"
    return f"music: {action}" + (f" {playlist}" if playlist else "")


@register(
    "force_quit_app",
    "Force-quit an unresponsive application (kill -9 equivalent) — for when "
    "quit_app doesn't work because the app has stopped responding. Unsaved "
    "work in that app is lost.",
    {"type": "object", "properties": {"name": {"type": "string", "description": "Application name."}}},
    category="app_control",
    aliases=["this app is frozen, kill it", "force quit safari",
             "photoshop isn't responding, close it", "the app is hung, make it stop"],
)
def force_quit_app(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return "(error: force_quit_app needs a `name`.)"
    p = _run(["pkill", "-9", "-if", n])
    if p.returncode not in (0, 1):
        return f"(error force-quitting {n}: {p.stderr.strip()})"
    if p.returncode == 1:
        return f"No running process matched {n!r}."
    return f"Force-quit {n}."


@register(
    "print_document",
    "Print a file using the default printer.",
    {"type": "object",
     "properties": {
         "path": {"type": "string", "description": "The file to print."},
         "copies": {"type": "integer", "description": "Number of copies. Default 1."},
     },
     "required": ["path"]},
    category="app_control",
    aliases=["print this document", "print this pdf", "I need a hard copy of this file"],
)
def print_document(path: str, copies: int = 1) -> str:
    from pathlib import Path

    p = Path(path).expanduser()
    if not p.exists():
        return f"(no such file: {p})"
    try:
        n = max(1, min(20, int(copies)))
    except (TypeError, ValueError):
        n = 1
    result = _run(["lp", "-n", str(n), str(p)])
    if result.returncode != 0:
        return f"(could not print {p.name}: {result.stderr.strip() or 'is a printer configured?'})"
    return f"Sent {p.name} to the printer ({n} {'copy' if n == 1 else 'copies'})."


@register(
    "manage_spaces",
    "Switch to a different macOS Space (virtual desktop) by number, or move "
    "left/right. Spaces have no name Wisp can read, only position.",
    {"type": "object",
     "properties": {
         "direction": {"type": "string", "enum": ["left", "right", "number"],
                       "description": "Move relative, or jump to a specific number."},
         "number": {"type": "integer", "description": "Space number, for direction='number'."},
     },
     "required": ["direction"]},
    category="app_control",
    aliases=["switch to my next desktop space", "go to desktop 2",
             "move one space to the left", "switch desktops"],
)
def manage_spaces(direction: str, number: int | None = None) -> str:
    d = (direction or "").strip().lower()
    key_codes = {"left": 123, "right": 124}
    if d in key_codes:
        script = ('tell application "System Events" to key code '
                  f'{key_codes[d]} using control down')
        p = _osascript(script)
        if p.returncode != 0:
            return f"(could not switch spaces: {p.stderr.strip()})"
        return f"Switched one space {d}."
    if d == "number":
        if number is None or not (1 <= int(number) <= 9):
            return "(error: direction='number' needs a `number` from 1-9.)"
        # Control-N jumps to Space N via the default Mission Control shortcuts.
        # ANSI keyboard key codes for the digit row (NOT numeric order — this
        # is the physical key layout, digits 7/8/9 sit at higher codes than 4-6).
        codes = {1: 18, 2: 19, 3: 20, 4: 21, 5: 23, 6: 22, 7: 26, 8: 28, 9: 25}
        script = ('tell application "System Events" to key code '
                  f'{codes[int(number)]} using control down')
        p = _osascript(script)
        if p.returncode != 0:
            return (f"(could not switch to space {number}: {p.stderr.strip()}. "
                    f"Check System Settings > Keyboard > Shortcuts > Mission "
                    f"Control has 'Switch to Desktop {number}' enabled.)")
        return f"Switched to space {number}."
    return f"(error: unknown direction {d!r}. Use left, right, or number.)"
