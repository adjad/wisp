"""Raw AppleScript execution, and scheduling a future one-shot action.

`run_applescript` is the escape hatch ABOVE run_shell for anything Wisp's
dedicated tools don't cover but the target app IS scriptable — the same
relationship run_shell has to "anything at all", just narrower and safer
(AppleScript can't touch the filesystem or network the way an arbitrary shell
command can).

`schedule_task` reuses the SAME persisted timer daemon `timers_alarms.py`
built (survives a backend restart, notifies via macOS notification) rather
than inventing a second scheduling mechanism — the only difference is what
fires: a plain timer notifies, this remembers a piece of TEXT to bring back to
the user's attention. It is NOT a way to have Wisp autonomously perform an
ARBITRARY future action (send an email later, etc.) — `schedule_send` already
exists for that one specific case, and building a general "run any tool
later, unattended" primitive is a materially bigger trust/safety surface than
this session should introduce without the user explicitly asking for it.

`set_hotkey` (global keyboard shortcut registration) needs an Accessibility-
level Swift bridge (CGEventTap or a global monitor) that doesn't exist —
deferred, not built, same honesty standard as speech_tools' two gaps.
"""
from __future__ import annotations

import subprocess
import time
import uuid

from service.tools.registry import register
from service.tools.timers_alarms import _TIMERS, _arm, _save

_TIMEOUT = 30


@register(
    "run_applescript",
    "Run raw AppleScript against a specific app — the escape hatch when the "
    "user's request is scriptable but no dedicated Wisp tool covers it yet. "
    "Prefer a real tool when one exists (music, calendar, mail, etc.); use "
    "this only for genuinely one-off app automation.",
    {"type": "object",
     "properties": {"script": {"type": "string", "description": "The AppleScript source to run."}},
     "required": ["script"]},
    category="shell",
    aliases=["run this applescript", "automate this app for me directly",
             "automate this in applescript"],
)
def run_applescript(script: str) -> str:
    src = (script or "").strip()
    if not src:
        return "(error: run_applescript needs a `script`.)"
    p = subprocess.run(["osascript", "-e", src], capture_output=True,
                       text=True, timeout=_TIMEOUT)
    if p.returncode != 0:
        return f"(script error: {(p.stderr or '').strip()})"
    return p.stdout.strip() or "(ran with no output)"


@register(
    "schedule_task",
    "Remind the user of something at a future time — reuses the same timer "
    "daemon set_timer does, so it survives a backend restart. This is a "
    "REMINDER, not a way to have Wisp autonomously perform an action later: "
    "for sending something later, use schedule_send instead.",
    {"type": "object",
     "properties": {
         "text": {"type": "string", "description": "What to be reminded of."},
         "minutes_from_now": {"type": "integer", "description": "How many minutes from now."},
     },
     "required": ["text", "minutes_from_now"]},
    category="timer_write",
    aliases=["ping me about this again later", "bring this back up in 30 minutes",
             "surface this again in a bit"],
)
def schedule_task(text: str, minutes_from_now: int) -> str:
    txt = (text or "").strip()
    if not txt:
        return "(error: schedule_task needs `text`.)"
    try:
        mins = max(1, min(1440, int(minutes_from_now)))
    except (TypeError, ValueError):
        return f"(error: {minutes_from_now!r} isn't a number of minutes.)"
    tid = uuid.uuid4().hex[:8]
    _TIMERS[tid] = {"kind": "timer", "label": txt,
                    "deadline": time.time() + mins * 60, "created": time.time()}
    _save()
    _arm(tid)
    return f"I'll bring up \"{txt}\" again in {mins} minute(s)."


@register(
    "set_hotkey",
    "Register a global keyboard shortcut. NOT YET AVAILABLE — this needs a "
    "native Accessibility-level event tap in the Swift app that doesn't "
    "exist yet. Says so rather than pretending to bind one.",
    {"type": "object",
     "properties": {
         "keys": {"type": "string", "description": "The key combination, e.g. 'cmd+shift+w'."},
         "action": {"type": "string", "description": "What it should do."},
     }},
    category="app_control",
    aliases=["set a global hotkey for this", "bind a keyboard shortcut to that",
             "I want a hotkey for this action"],
)
def set_hotkey(keys: str = "", action: str = "") -> str:
    return ("Global hotkeys aren't built yet — they need a native "
            "Accessibility-level event tap in the Swift app that doesn't "
            "exist today. You can set app-specific shortcuts in each app's "
            "own preferences, or system-wide ones in System Settings > "
            "Keyboard > Keyboard Shortcuts.")
