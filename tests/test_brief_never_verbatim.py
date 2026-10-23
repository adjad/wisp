"""A brief is evidence for the model, never the answer itself.

Found on a real clean install (build 2016): before Calendar had synced, "give
me my daily brief" returned the raw evidence ("Calendar unknown: ... no cached
absence is verified") in 0.4 s, because one unverified section classed the
whole read as no_match and a no_match direct read was shown verbatim.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402
from service.agent.loop import classify_tool_outcome  # noqa: E402

EVIDENCE = (
    "Good morning — Sunday, October 18.\n\nSchedule:\nCalendar unknown: missing, stale, "
    "partial, failed or insufficient date coverage; no cached absence is verified.\n\n"
    "Reminders:\n- overdue: Renew passport — 2026-10-02 18:15\n"
    "Undated incomplete reminders (no due date; neither overdue nor upcoming):\n"
    "No matches in the current undated snapshot.\n")


class Narrator:
    def __init__(self):
        self.calls = 0

    async def ensure_only(self, *args, **kwargs):
        return None

    async def stream_events(self, model, messages, **kwargs):
        self.calls += 1
        yield {"kind": "content", "text": "NARRATED"}
        yield {"kind": "final", "message": {"role": "assistant", "content": "NARRATED",
                                           "tool_calls": None}}


class Approver:
    async def confirm(self, action):
        return True


async def emit(ev):
    return None


def test_unverified_brief_is_narrated_not_shown_raw():
    from service.router import router
    from service.tools.registry import REGISTRY
    assert classify_tool_outcome("daily_brief", EVIDENCE).status == "no_match"
    d = asyncio.run(router.route("give me my daily brief"))
    assert [n for n, _ in d.direct_calls] == ["daily_brief"]
    client = Narrator()
    tool = REGISTRY["daily_brief"]
    with patch.object(tool, "func", lambda **_: EVIDENCE):
        out = asyncio.run(loop.run_agent(
            client, "test-model", [{"role": "user", "content": "give me my daily brief"}],
            emit, Approver(), tools=d.tool_subset, direct_calls=d.direct_calls,
            narration_after=d.narration_after, required_tool_groups=d.required_tool_groups,
            forbidden_tools=d.forbidden_tools, tool_argument_bindings=d.tool_argument_bindings,
            multi_round=d.multi_round))
    assert "no cached absence" not in out, out
    assert client.calls >= 1 and "NARRATED" in out, out


if __name__ == "__main__":
    test_unverified_brief_is_narrated_not_shown_raw()
    print("ok")
