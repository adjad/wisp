"""Strict regressions for the failures in the 2026-09-02 debug export."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from service.agent import loop
from service.router.router import route
from service.tools.registry import REGISTRY, Tool
from service.tools import email_tools


class ScriptedClient:
    def __init__(self, calls):
        self.script = iter(calls)
        self.requests = 0

    async def ensure_only(self, *args, **kwargs):
        return None

    async def stream_events(self, model, messages, **kwargs):
        self.requests += 1
        name, args = next(self.script)
        message = {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"call-{self.requests}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}
        yield {"kind": "final", "message": message}


class DenyingApprover:
    def __init__(self):
        self.seen = []

    async def confirm(self, action):
        self.seen.append(action)
        return False


async def _run(client, prompt, *, tools, direct_calls=None, bindings=None,
               required=(), force=None):
    events = []

    async def emit(event):
        events.append(event)

    approver = DenyingApprover()
    answer = await loop.run_agent(
        client, "test-model", [{"role": "user", "content": prompt}], emit,
        approver, tools=tools, direct_calls=direct_calls,
        tool_argument_bindings=bindings, required_tool_groups=required,
        force_first_tool=force, max_steps=5)
    return answer, approver, events


def test_named_day_parts_are_bound_to_standard_times_without_clarification():
    cases = {
        "Remind me tomorrow morning to call Mom": "09:00",
        "Remind me tomorrow afternoon to call Mom": "15:00",
        "Remind me tomorrow evening to call Mom": "18:00",
        "Remind me tomorrow night to call Mom": "20:00",
        "Create a reminder tomorrow to send my vaccine report to UCSC": "09:00",
    }
    for prompt, clock in cases.items():
        decision = asyncio.run(route(prompt))
        assert decision.reminder_action == "create", prompt
        assert decision.force_first_tool == "add_reminder", prompt
        assert decision.tool_argument_bindings["add_reminder"]["when_iso"].endswith(clock), prompt
        assert "add_reminder" not in decision.forbidden_tools, prompt


def test_canvas_tonight_and_messages_is_a_two_effect_contract():
    prompt = ("Create a reminder for me to finish a Canvas assignment by tonight "
              "and tell Mom about it too through Messages.")
    decision = asyncio.run(route(prompt))
    groups = [set(group) for group in decision.required_tool_groups]
    assert {"add_reminder"} in groups
    assert {"lookup_contact"} in groups
    assert {"send_message"} in groups
    assert decision.tool_argument_bindings["add_reminder"]["when_iso"].endswith("20:00")
    assert not decision.clarify_channel
    assert "send_email" not in (decision.tool_subset or [])


def test_denied_message_or_email_terminates_after_one_confirmation():
    # The scripts deliberately contain three identical retries. Only the first
    # may be consumed after the user denies it.
    cases = [
        ("Send Mom a test message", "send_message",
         {"to": "Mom", "text": "Test message"}),
        ("Email johnstandark@gmail.com with subject Test and body Hello",
         "send_email", {"to": "johnstandark@gmail.com", "subject": "Test",
                        "body": "Hello"}),
    ]
    for prompt, tool, args in cases:
        call = (tool, args)
        client = ScriptedClient([call, call, call])
        answer, approver, events = asyncio.run(_run(
            client, prompt, tools=[tool], required=(frozenset({tool}),)))
        assert answer == "Okay — I didn’t send it."
        assert client.requests == 1
        assert len(approver.seen) == 1
        assert sum(e.get("type") == "tool_call" for e in events) == 1
        assert events[-1]["type"] == "done"


def test_playstation_lookup_is_strict_and_empty_result_bypasses_model():
    prompt = "Can you check my email for purchases from PlayStation?"
    decision = asyncio.run(route(prompt))
    assert decision.direct_calls == [("view_emails", {
        "query": "PlayStation", "count": 10, "strict_match": True})]

    real = REGISTRY["view_emails"]
    ran = []

    async def empty_lookup(query=None, count=5, strict_match=False):
        ran.append({"query": query, "count": count, "strict_match": strict_match})
        return "No emails matching “PlayStation” were found in the recent inbox Wisp searched."

    REGISTRY["view_emails"] = Tool(
        name="view_emails", description="fixture", parameters={
            "type": "object", "properties": {
                "query": {"type": "string"}, "count": {"type": "integer"},
                "strict_match": {"type": "boolean"},
            }},
        category="email_read", func=empty_lookup)
    try:
        client = ScriptedClient([])
        answer, _, events = asyncio.run(_run(
            client, prompt, tools=decision.tool_subset,
            direct_calls=decision.direct_calls))
    finally:
        REGISTRY["view_emails"] = real

    assert ran == [{"query": "PlayStation", "count": 10, "strict_match": True}]
    assert client.requests == 0
    assert answer.startswith("No emails matching “PlayStation”")
    assert events[-1]["type"] == "done"


def test_strict_email_lookup_returns_no_match_instead_of_unrelated_mail():
    unrelated = [{
        "account": "Google", "sender": "store@example.com", "to": "Adi",
        "subject": "A different receipt", "body": "Nothing about that vendor",
        "ts": 1.0, "message_id": "fixture", "unread": False,
    }]
    with (patch.object(email_tools, "_ensure_email_cache", new=AsyncMock()),
          patch.object(email_tools, "_purge_raw_if_expired"),
          patch.object(email_tools, "_raw_emails", "fixture"),
          patch.object(email_tools, "_parse_raw", return_value=unrelated)):
        answer = asyncio.run(email_tools.view_emails_impl(
            query="PlayStation", count=10, strict_match=True))
    assert answer == "No emails matching “PlayStation” were found in the recent inbox Wisp searched."


def test_literal_email_address_is_bound_without_contact_lookup():
    prompt = ("Email johnstandark@gmail.com with subject Wisp routing test and "
              "body This is a routing test.")
    decision = asyncio.run(route(prompt))
    assert frozenset({"send_email"}) in decision.required_tool_groups
    assert decision.tool_argument_bindings["send_email"]["to"] == "johnstandark@gmail.com"
    assert "lookup_contact" not in (decision.tool_subset or [])
