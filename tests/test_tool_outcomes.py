"""Typed tool-result compatibility and action fingerprints.

    .venv/bin/python tests/test_tool_outcomes.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent.loop import _action_fingerprint  # noqa: E402
from service.tools.registry import classify_tool_outcome  # noqa: E402


def main() -> int:
    checks = {
        "planned is not success": classify_tool_outcome(
            "send_email", "[TEST MODE]", planned=True).status == "planned",
        "denial is typed": classify_tool_outcome(
            "send_email", "The user denied this action.", denied=True).status == "denied",
        "explicit non-send is failure": classify_tool_outcome(
            "send_email", "(NOT sent — incomplete body)").status == "failed",
        "successful send has sent effect": (
            (o := classify_tool_outcome("send_message", "Message sent to Mom.")).status == "succeeded"
            and o.effect == "sent"),
        "fingerprint is stable": _action_fingerprint("send_message", {"to": "Mom", "text": "Hi"})
        == _action_fingerprint("send_message", {"text": "Hi", "to": "Mom"}),
        "fingerprint changes with approved content": _action_fingerprint(
            "send_message", {"to": "Mom", "text": "Hi"}) != _action_fingerprint(
                "send_message", {"to": "Mom", "text": "Bye"}),
    }
    failed = [name for name, ok in checks.items() if not ok]
    for name, ok in checks.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    print(f"\n{len(checks) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
