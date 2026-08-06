"""App-control tools: open / quit apps and drive Spotify & Music.

All carry the `app_control` safety category, so the policy engine gates them:
in view-only mode they're denied; otherwise they require the user's confirmation
(see service/safety/policy.py). Registered on import.
"""
from __future__ import annotations

import subprocess

from service.tools.registry import register


def _as_string(value: str) -> str:
    """Quote a value as an AppleScript string literal.

    This used to be `shlex.quote`, which is a POSIX *shell* quoter and the wrong
    tool twice over. It emits single quotes, which AppleScript rejects outright
    — so `quit_app("Google Chrome")` produced
    `tell application 'Google Chrome' to quit` and failed with a syntax error on
    every app whose name contains a space. Single-word names only worked by
    accident, because shlex.quote leaves them bare and AppleScript happens to
    accept a bare identifier there.

    AppleScript strings are double-quoted, with backslash and double-quote as
    the only escapes. Doing it properly fixes the bug and closes the injection
    route in the same change: these names reach here from the model, which can
    be steered by whatever text it just read.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _osascript(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=20)


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
    p = _osascript(f'tell application {_as_string(name)} to quit')
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
        script = f'tell application "Spotify" to play track {_as_string(uri)}'
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
        script = f'tell application "Music" to play playlist {_as_string(playlist)}'
    elif action in verbs:
        script = f'tell application "Music" to {verbs[action]}'
    else:
        return f"(unknown music action: {action})"
    p = _osascript(script)
    if p.returncode != 0:
        return f"(music error: {p.stderr.strip() or 'is Music installed?'})"
    return f"music: {action}" + (f" {playlist}" if playlist else "")
