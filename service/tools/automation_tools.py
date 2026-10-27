"""Raw AppleScript execution.

`run_applescript` is the escape hatch for anything Wisp's dedicated tools
don't cover but the target app IS scriptable. It is not narrower than
run_shell: one line can send an iMessage or a Mail message as the user, and
`do shell script` is a full shell. So the policy confirms it on every call,
full access or not (policy.ALWAYS_CONFIRM_TOOLS), and the card shows the script.
"""
from __future__ import annotations

import subprocess

from service.tools.registry import register

_TIMEOUT = 30


@register(
    "run_applescript",
    "Run raw AppleScript against a specific app — the escape hatch when the "
    "user's request is scriptable but no dedicated Wisp tool covers it yet. "
    "Prefer a real tool when one exists (calendar, mail, etc.); use "
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
    from service.safety.policy import Tier, decide
    src = (script or "").strip()
    if not src:
        return "(error: run_applescript needs a `script`.)"
    # Recheck at execution time, like run_shell: a direct call or a mode change
    # while the card was open must still meet the deny floor. CONFIRM was the
    # caller's to fulfil.
    decision = decide("shell", {"script": src}, tool="run_applescript")
    if decision.tier is Tier.DENY:
        return f"(error: blocked by safety policy: {decision.reason})"
    from service.identity import child_environment
    p = subprocess.run(["osascript", "-e", src], capture_output=True,
                       text=True, timeout=_TIMEOUT, env=child_environment())
    if p.returncode != 0:
        return f"(script error: {(p.stderr or '').strip()})"
    return BuiltinCompletion(p.stdout.strip() or "(ran with no output)",
                             tool_name="run_applescript", completion_code=p.returncode)


