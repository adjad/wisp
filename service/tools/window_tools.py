"""Windows, spaces, and switching between what's running.

These drive the Accessibility API through System Events, which is why they all
share one permission story: the first call fails with error -1719 until Wisp is
granted Accessibility access. Every tool here detects that specific code and
says which checkbox to tick, rather than reporting "unknown error" for the one
failure that is entirely fixable by the user.

`window_control` is one tool with an action enum rather than five (close,
minimize, maximize, fullscreen, tile). Same reasoning as `toggle_setting`:
five schemas differing only by a verb are what a small model picks wrong from,
and each costs tokens in every menu it appears in.
"""
from __future__ import annotations

import subprocess

from service.tools.registry import register

_TIMEOUT = 20


def _osa(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=_TIMEOUT)


def _as_str(value: str) -> str:
    """AppleScript string literal — double quotes with backslash escapes.
    (See apps._as_str for the incident behind not using shlex.quote here.)"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _guard(p: subprocess.CompletedProcess) -> str | None:
    if p.returncode == 0:
        return None
    msg = (p.stderr or "").strip()
    if "-1719" in msg or "assistive" in msg.lower() or "not allowed" in msg.lower():
        return ("(Wisp needs Accessibility access to control windows. Grant it in "
                "System Settings > Privacy & Security > Accessibility, then try again.)")
    if "-1728" in msg:
        return "(That app has no window open right now.)"
    return f"(error: {msg or 'unknown error'})"


@register(
    "list_running_apps",
    "List the applications currently running and which one is in front. Use "
    "this to answer 'what do I have open' or before switching to something.",
    {"type": "object", "properties": {}},
    category="app_control",
    aliases=["what do I have open", "what's running right now",
             "which programs are running right now", "what am I looking at",
             "show me everything that's open"],
)
def list_running_apps() -> str:
    p = _osa('tell application "System Events" to get the name of every '
             'application process whose background only is false')
    if (e := _guard(p)) is not None:
        return e
    names = sorted(n.strip() for n in p.stdout.split(",") if n.strip())
    if not names:
        return "Nothing is running."
    f = _osa('tell application "System Events" to get the name of the first '
             'application process whose frontmost is true')
    front = f.stdout.strip() if f.returncode == 0 else ""
    listed = ", ".join(names)
    return f"{len(names)} apps open: {listed}." + (f" Frontmost: {front}." if front else "")


@register(
    "switch_app",
    "Bring a running application to the front, or hide everything except it. "
    "Unlike open_app this does not launch anything — use it to move between "
    "things already open.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The application to bring forward."},
            "hide_others": {"type": "boolean",
                            "description": "Also hide every other app, for focus."},
        },
        "required": ["name"],
    },
    category="app_control",
    aliases=["switch over to safari", "bring chrome to the front",
             "go back to my editor", "hide everything except slack",
             "put xcode in front"],
)
def switch_app(name: str, hide_others: bool = False) -> str:
    name = (name or "").strip()
    if not name:
        return "(error: switch_app needs the app `name`.)"
    p = _osa(f'tell application {_as_str(name)} to activate')
    if (e := _guard(p)) is not None:
        return e
    if hide_others:
        h = _osa('tell application "System Events" to set visible of '
                 f'(every process whose name is not {_as_str(name)} '
                 'and background only is false) to false')
        if h.returncode != 0:
            return f"Switched to {name}, but couldn't hide the others."
        return f"Switched to {name} and hid everything else."
    return f"Switched to {name}."


@register(
    "window_control",
    "Act on the frontmost window: close, minimize, zoom (maximize), enter or "
    "exit full screen, or tile it to the left or right half of the screen.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["close", "minimize", "zoom", "fullscreen",
                                "exit_fullscreen", "left", "right"],
                       "description": "What to do with the front window."},
            "app": {"type": "string",
                    "description": "Optional app name. Defaults to whatever is frontmost."},
        },
        "required": ["action"],
    },
    category="app_control",
    aliases=["close this window", "minimize that", "make this full screen",
             "snap this to the left half", "put these side by side",
             "get this window out of my way", "maximize the window"],
)
def window_control(action: str, app: str = "") -> str:
    action = (action or "").strip().lower()
    valid = {"close", "minimize", "zoom", "fullscreen", "exit_fullscreen",
             "left", "right"}
    if action not in valid:
        return f"(error: unknown action {action!r}. Use one of: {', '.join(sorted(valid))}.)"

    target = (f"process {_as_str(app.strip())}" if app.strip()
              else "(first application process whose frontmost is true)")

    if action in ("left", "right"):
        # Tiling has no AppleScript verb; it is a position+size write on the
        # window, which is why this needs the screen's usable bounds rather than
        # its pixel dimensions — subtracting the menu bar is what stops the
        # window sitting underneath it.
        script = (
            'tell application "Finder" to set screenBounds to bounds of window of desktop\n'
            'set screenW to item 3 of screenBounds\n'
            'set screenH to item 4 of screenBounds\n'
            'set menuBar to 25\n'
            'tell application "System Events"\n'
            f'  set win to front window of {target}\n'
            f'  set position of win to {{{"0" if action == "left" else "screenW / 2"}, menuBar}}\n'
            '  set size of win to {screenW / 2, screenH - menuBar}\n'
            'end tell')
        p = _osa(script)
        if (e := _guard(p)) is not None:
            return e
        return f"Tiled the window to the {action} half."

    verb = {
        "close": 'click button 1 of front window of {t}',
        "minimize": 'set value of attribute "AXMinimized" of front window of {t} to true',
        "zoom": 'click button 2 of front window of {t}',
        "fullscreen": 'set value of attribute "AXFullScreen" of front window of {t} to true',
        "exit_fullscreen": 'set value of attribute "AXFullScreen" of front window of {t} to false',
    }[action]
    p = _osa(f'tell application "System Events"\n  {verb.format(t=target)}\nend tell')
    if (e := _guard(p)) is not None:
        return e
    said = {"close": "Closed", "minimize": "Minimized", "zoom": "Zoomed",
            "fullscreen": "Entered full screen for", "exit_fullscreen": "Exited full screen for"}
    return f"{said[action]} the {'front' if not app.strip() else app.strip()} window."


@register(
    "reveal_in_finder",
    "Show a file or folder in Finder, selected and ready to drag — as opposed "
    "to opening it. Use after find_files when the user wants to SEE where "
    "something lives.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The file or folder to reveal."},
        },
        "required": ["path"],
    },
    category="app_control",
    aliases=["show me where that file lives", "open the folder that's in",
             "reveal this in finder", "take me to that file"],
)
def reveal_in_finder(path: str) -> str:
    from pathlib import Path

    target = Path((path or "").strip()).expanduser()
    if not target.exists():
        return f"(error: {path} doesn't exist on this Mac.)"
    p = subprocess.run(["open", "-R", str(target)], capture_output=True,
                       text=True, timeout=_TIMEOUT)
    if p.returncode != 0:
        return f"(could not reveal it: {(p.stderr or '').strip()})"
    return f"Showing {target.name} in Finder."
