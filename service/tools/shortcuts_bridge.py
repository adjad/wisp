"""Apple Shortcuts — the escape hatch for anything a shipped tool doesn't
cover. `shortcuts` is a real macOS CLI (`/usr/bin/shortcuts`), keyless, and
already installed; verified live on this machine (`shortcuts list`).

This is deliberately thin: Wisp doesn't author Shortcuts (Shortcuts Editor has
no CLI/scripting surface to drive), it runs ones the user already built or
installed. `install_shortcut` opens a .shortcut file for the user to review and
accept in the Shortcuts app's own import UI — Wisp never silently installs an
automation that can run arbitrary actions on the user's behalf.
"""
from __future__ import annotations

import subprocess

from service.tools.registry import register

_TIMEOUT = 60


@register(
    "list_shortcuts",
    "List the user's installed Apple Shortcuts, so you know what's available "
    "before suggesting run_shortcut.",
    {"type": "object", "properties": {}},
    category="app_control",
    aliases=["what shortcuts do I have", "list my shortcuts",
             "show me my automations", "what shortcuts are installed"],
)
def list_shortcuts() -> str:
    p = subprocess.run(["shortcuts", "list"], capture_output=True,
                       text=True, timeout=_TIMEOUT)
    if p.returncode != 0:
        return f"(could not list shortcuts: {(p.stderr or '').strip()})"
    names = [n for n in p.stdout.splitlines() if n.strip()]
    if not names:
        return "No Shortcuts installed."
    return f"{len(names)} shortcut(s):\n" + "\n".join(f"  {n}" for n in names)


@register(
    "run_shortcut",
    "Run one of the user's installed Apple Shortcuts by name — the escape "
    "hatch for anything Wisp doesn't have a dedicated tool for yet, as long "
    "as the user has built or installed a Shortcut for it. Use list_shortcuts "
    "first if you're not sure of the exact name.",
    {"type": "object",
     "properties": {
         "name": {"type": "string", "description": "Exact name of the Shortcut to run."},
         "input": {"type": "string", "description": "Optional text input to pass to the Shortcut."},
     },
     "required": ["name"]},
    category="app_control",
    aliases=["run my morning shortcut", "trigger the wifi shortcut",
             "run the shortcut called Focus Mode", "execute my custom automation"],
)
def run_shortcut(name: str, input: str = "") -> str:  # noqa: A002
    n = (name or "").strip()
    if not n:
        return "(error: run_shortcut needs a `name`.)"
    argv = ["shortcuts", "run", n]
    kwargs = {}
    if input:
        kwargs["input"] = input
    p = subprocess.run(argv, capture_output=True, text=True,
                       timeout=_TIMEOUT, **kwargs)
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        # macOS's own error text uses a CURLY apostrophe (U+2019 — "Couldn't
        # find shortcut"), which a straight-quote "couldn't" check silently
        # never matches — verified live, every not-found error fell through
        # to the generic branch below instead of the friendly one. Matching
        # on "find shortcut" alone sidesteps the quote character entirely.
        low = err.lower()
        if "find shortcut" in low or "not found" in low or not err:
            return (f"No Shortcut named {n!r}. Use list_shortcuts to see what's "
                    f"actually installed — the name has to match exactly.")
        return f"(the Shortcut ran into a problem: {err})"
    out = p.stdout.strip()
    return f"Ran {n!r}." + (f" Output: {out}" if out else "")


@register(
    "install_shortcut",
    "Open a .shortcut file so the user can review and install it themselves. "
    "Wisp does not silently install a Shortcut — it can run arbitrary actions, "
    "so the user approves it in the Shortcuts app's own import screen.",
    {"type": "object",
     "properties": {"path": {"type": "string", "description": "Path to the .shortcut file."}},
     "required": ["path"]},
    category="app_control",
    aliases=["install this shortcut file", "add this shortcut to my library",
             "install this .shortcut file into shortcuts"],
)
def install_shortcut(path: str) -> str:
    from pathlib import Path

    p = Path(path).expanduser()
    if not p.exists():
        return f"(no such file: {p})"
    if p.suffix != ".shortcut":
        return f"(error: {p.name} doesn't look like a .shortcut file.)"
    result = subprocess.run(["open", str(p)], capture_output=True,
                            text=True, timeout=15)
    if result.returncode != 0:
        return f"(could not open {p.name}: {(result.stderr or '').strip()})"
    return (f"Opened {p.name} in Shortcuts — review what it does and tap "
            f"'Add Shortcut' to install it.")
