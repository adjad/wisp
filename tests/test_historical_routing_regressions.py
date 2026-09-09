from __future__ import annotations

import json
import unittest
from pathlib import Path

import service.tools  # noqa: F401
from service.router.router import route


FIXTURE = (Path(__file__).resolve().parent.parent /
           "test_fixtures/routing_regressions/historical_prompts.json")


class HistoricalRoutingRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_every_historical_prompt_has_a_strict_safe_route(self) -> None:
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        failures: list[str] = []
        for case in cases:
            decision = await route(
                case["prompt"], last_assistant=case.get("last_assistant"),
                last_tools=case.get("last_tools"))
            offered = set(decision.tool_subset or [])
            missing = [g for g in case.get("required_tool_groups", [])
                       if not offered.intersection(g)]
            forbidden = offered.intersection(case.get("forbidden_tools", []))
            wrong_tools = case.get("expect_tools") is False and decision.needs_tools
            wrong_reminder = (
                "expected_reminder_action" in case and
                decision.reminder_action != case["expected_reminder_action"])
            wrong_channel = (
                "expected_clarify_channel" in case and
                decision.clarify_channel is not case["expected_clarify_channel"])
            if missing or forbidden or wrong_tools or wrong_reminder or wrong_channel:
                failures.append(
                    f"{case['id']}: missing={missing}, forbidden={sorted(forbidden)}, "
                    f"needs_tools={decision.needs_tools}, "
                    f"reminder_action={decision.reminder_action!r}, "
                    f"clarify_channel={decision.clarify_channel!r}")
        self.assertEqual([], failures, "\n".join(failures))

