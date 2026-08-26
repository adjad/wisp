"""Corpus shape/safety checks; requires no pytest or model server."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.router_adversarial_cases import CASES, validate  # noqa: E402


def main() -> int:
    errors = validate()
    assert not errors, "\n".join(errors)
    assert len(CASES) >= 68, f"expected full corpus, got {len(CASES)}"

    risky = {
        "add_calendar_event", "add_reminder", "cancel_event", "cancel_scheduled_send",
        "complete_reminder", "create_note", "delete_path", "draft_email", "draft_message",
        "forget", "mark_email_read", "move_path", "remember", "schedule_send", "send_email",
        "send_message", "set_volume", "toggle_setting", "trash_file", "update_event", "write_file",
    }
    for case in CASES:
        if (set(case.required) | set(case.one_of)) & risky:
            assert case.mode == "plan", f"{case.id}: mutating prompt must use plan mode"

    plan_count = sum(case.mode == "plan" for case in CASES)
    print(f"ok: {len(CASES)} prompts validated ({plan_count} intercepted plan-mode cases)")
    print("ok: corpus test imported no model or assistant tool runtime")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
