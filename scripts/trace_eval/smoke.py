#!/usr/bin/env python3
"""One offline end-to-end adapter check with Wisp's real agent loop.

Uses a scripted model and synthetic get_upcoming result. No inference socket,
native tool, message send, or user data is involved.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts import run_routing_stress_suite as stress
from scripts.trace_eval import run as replay
from scripts.trace_eval.core import load_cases


class ScriptedModel:
    def __init__(self):
        self.steps = 0

    async def stream_events(self, model, messages, **kwargs):
        self.steps += 1
        if self.steps == 1:
            yield {"kind": "final", "message": {"role": "assistant", "content": "",
                   "tool_calls": [{"id": "smoke-1", "function": {
                       "name": "get_upcoming",
                       "arguments": '{"period":"today","calendar_only":true}'}}]}}
        else:
            answer = "There is a meeting at 2:00 PM, so I did not send a message."
            yield {"kind": "content", "text": answer}
            yield {"kind": "final", "message": {"role": "assistant",
                   "content": answer, "tool_calls": None}}


async def main() -> None:
    from service.router.router import RouteDecision

    case = load_cases(Path("test_fixtures/trace_eval/dev_cases.json"))[0]
    os.environ["TZ"] = "America/Los_Angeles"
    if hasattr(time, "tzset"):
        time.tzset()
    stress.CLOCK = datetime(2026, 9, 22, 10, 0,
                            tzinfo=ZoneInfo("America/Los_Angeles"))
    stress.EMAIL = "fixture-user@example.test"
    stress.PHONE = "+1-202-555-0142"
    output = Path(".evo/trace_eval/integration_smoke").resolve()
    home = output / "isolated_home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(replay.synthetic_model_overlay(
        "Ling-3.0-tiny-oQ6e"))
    stress.bootstrap(output,
                     "Ling-3.0-tiny-oQ6e", copy_user_context=False)
    from service import config
    assert config.models_config()["roles"]["agent"] == "Ling-3.0-tiny-oQ6e"
    from service.memory import identity
    identity.user_name = lambda: "Fixture User"
    identity.user_emails = lambda: [stress.EMAIL]
    from service.memory import prompt_blocks
    prompt_blocks.memory_block = lambda **kwargs: ""
    prompt_blocks.now_line = lambda **kwargs: (
        "\nThe current date and time is Tuesday, September 22, 2026 at 10:00 AM, "
        "America/Los_Angeles.")

    async def fake_route(*args, **kwargs):
        return RouteDecision(role="assistant", model="Ling-3.0-tiny-oQ6e",
                             needs_tools=True, source="rules", reason="synthetic smoke",
                             tool_subset=["get_upcoming"],
                             force_first_tool="get_upcoming", expect_tool_first=True)

    stress.RUNTIME["route"] = fake_route
    stress.fixture = replay.fixture
    original_new_state = stress.new_state

    def state_with_receipts(c):
        state = original_new_state(c)
        state["fixture_mismatches"] = []
        state["fixture_receipts"] = []
        replay._LAST_STATE = state
        return state

    stress.new_state = state_with_receipts
    trace, verdict = await replay.run_one(case, stress.MeasuredClient(ScriptedModel()), 20)
    if trace.get("error"):
        raise RuntimeError(f"offline replay failed: {trace['error']}")
    assert verdict["status"] == "UNVERIFIED", verdict
    assert not verdict["failures"], verdict
    assert len(trace["model_steps"]) >= 2
    assert [d["name"] for d in trace["dispatches"]] == ["get_upcoming"]
    print("PASS offline Wisp agent-loop replay; semantic review remains required")


if __name__ == "__main__":
    asyncio.run(main())
