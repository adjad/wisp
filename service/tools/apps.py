"""App-control tools: open and quit apps.

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


# Spoken names that differ from the installed app's name ("Apple TV" is "TV").
_APP_NAME_ALIASES = {
    "disney plus": "Disney+", "hbo max": "Max", "amazon prime": "Prime Video",
    "apple tv": "TV", "apple tv+": "TV",
}


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
    name = _APP_NAME_ALIASES.get((name or "").strip().lower(), name)
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




