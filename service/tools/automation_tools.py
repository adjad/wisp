"""Raw AppleScript execution.

`run_applescript` is the escape hatch ABOVE run_shell for anything Wisp's
dedicated tools don't cover but the target app IS scriptable — the same
relationship run_shell has to "anything at all", just narrower and safer
(AppleScript can't touch the filesystem or network the way an arbitrary shell
command can).
"""
from __future__ import annotations

import subprocess

from service.tools.registry import register

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
    from service.router.model_led import BuiltinCompletion
    src = (script or "").strip()
    if not src:
        return "(error: run_applescript needs a `script`.)"
    p = subprocess.run(["osascript", "-e", src], capture_output=True,
                       text=True, timeout=_TIMEOUT)
    if p.returncode != 0:
        return f"(script error: {(p.stderr or '').strip()})"
    return BuiltinCompletion(p.stdout.strip() or "(ran with no output)",
                             tool_name="run_applescript", completion_code=p.returncode)


