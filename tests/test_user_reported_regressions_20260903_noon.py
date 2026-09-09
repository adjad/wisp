"""Strict regressions for failures reported in the 2026-09-03 noon debug export.

All execution tests replace external effects and data sources with local fakes.
They must never send a real message or change the user's reminders.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from service.agent import loop
from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tools import assistant_tools, web_tools
from service.tools.registry import REGISTRY, Tool
from service.workflows.compiler import (
    compile_decision,
    compile_new,
    extract_recipient,
    extract_stock_symbols,
)
from service.workflows.engine import prepare_turn


TRISHE_PROMPT = "send a message to trishe with my email summaries and calender for this month"
STOCK_PROMPT = "send a message to dad with my stock price updates"
STOCK_REPLY = "nvidia, google, AMD, and micron"
VACCINE_PROMPT = "draft an imessage to send to mom with my vaccine information from reminders about when it is"


class _NeverModelClient:
    calls = 0

    async def ensure_only(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("the model must not run after a required source failure")

    async def stream_events(self, *args, **kwargs):
        raise AssertionError("the model must not run after a required source failure")
        yield


class _CaptureClient:
    def __init__(self) -> None:
        self.messages = None

    async def ensure_only(self, *args, **kwargs):
        return None

    async def stream_events(self, model, messages, **kwargs):
        self.messages = messages
        yield {"kind": "final", "message": {
            "role": "assistant", "content": "Done.", "tool_calls": None}}


class _Approver:
    async def confirm(self, action):
        raise AssertionError(f"unexpected confirmation: {action}")


async def _emit_to(events: list[dict], event: dict) -> None:
    events.append(event)


def test_lowercase_named_recipient_is_preserved_without_reasking():
    plan = compile_new(TRISHE_PROMPT)
    assert plan is not None
    assert plan.status == "ready"
    assert plan.recipient == "trishe"
    assert plan.channel == "messages"
    assert plan.sources == ["calendar", "email"]

    decision = compile_decision(plan)
    assert decision.tool_subset == [
        "get_upcoming", "summarize_emails", "lookup_contact", "send_message"]
    assert decision.direct_calls == [
        ("get_upcoming", {"period": "this month"}),
        ("summarize_emails", {"period": "this month"}),
        ("lookup_contact", {"name": "trishe"}),
    ]
    assert decision.tool_argument_bindings["send_message"] == {"to": "trishe"}


def test_lowercase_recipient_survives_persistent_workflow_entry():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions.db")
        sid = store.create_session()
        turn = prepare_turn(store, sid, TRISHE_PROMPT)
    assert turn is not None and turn.decision is not None
    assert turn.response == ""
    assert turn.plan.status == "running"
    assert turn.plan.recipient == "trishe"


def test_recipient_parser_does_not_treat_payload_as_a_person():
    assert extract_recipient("send a message with my calendar", "messages") == ""
    assert extract_recipient("send my calendar summary", "messages") == ""


def test_mixed_case_stock_reply_keeps_every_requested_company():
    expected = ["NVDA", "GOOGL", "AMD", "MU"]
    assert extract_stock_symbols(STOCK_REPLY, standalone=True) == expected

    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions.db")
        sid = store.create_session()
        first = prepare_turn(store, sid, STOCK_PROMPT)
        assert first is not None
        assert first.response == "Which stock symbols or company names should I include?"
        second = prepare_turn(store, sid, STOCK_REPLY)
    assert second is not None and second.decision is not None
    assert second.plan.stock_symbols == expected
    assert second.decision.direct_calls[0] == (
        "get_stock_price", {"symbols": expected})
    assert second.decision.tool_argument_bindings["get_stock_price"] == {
        "symbols": expected}


def test_stock_lookup_is_parallel_and_all_or_none():
    called: list[str] = []

    async def fake_quote(symbol: str) -> str:
        called.append(symbol)
        await asyncio.sleep(0.03)
        if symbol == "google":
            return "(symbol search for 'google' failed: HTTP 503)"
        return f"{symbol}: $100.00 USD"

    started = time.perf_counter()
    with patch.object(web_tools, "_one_quote", side_effect=fake_quote):
        result = asyncio.run(web_tools.get_stock_price(
            ["nvidia", "google", "AMD", "micron"]))
    elapsed = time.perf_counter() - started

    assert set(called) == {"nvidia", "google", "AMD", "micron"}
    assert result.startswith("(error: incomplete stock lookup;")
    assert "Failed symbols: google" in result
    assert elapsed < 0.09, f"stock calls appear sequential ({elapsed:.3f}s)"


def test_incomplete_stock_source_never_reaches_send_or_model():
    original_stock = REGISTRY["get_stock_price"]
    original_send = REGISTRY["send_message"]
    sends: list[dict] = []

    async def fake_stock(symbols: list[str]) -> str:
        return "(error: incomplete stock lookup; Failed symbols: google.)"

    async def fake_send(to: str, text: str) -> str:
        sends.append({"to": to, "text": text})
        return "Message sent."

    REGISTRY["get_stock_price"] = Tool(
        "get_stock_price", "fake stock", {"type": "object", "properties": {
            "symbols": {"type": "array", "items": {"type": "string"}}},
            "required": ["symbols"]}, "web_read", fake_stock)
    REGISTRY["send_message"] = Tool(
        "send_message", "fake send", {"type": "object", "properties": {
            "to": {"type": "string"}, "text": {"type": "string"}},
            "required": ["to", "text"]}, "messages_send", fake_send)
    events: list[dict] = []
    client = _NeverModelClient()
    try:
        answer = asyncio.run(loop.run_agent(
            client, "test-model", [{"role": "user", "content": STOCK_PROMPT}],
            lambda event: _emit_to(events, event), _Approver(),
            tools=["get_stock_price", "send_message"],
            direct_calls=[("get_stock_price", {
                "symbols": ["nvidia", "google", "AMD", "micron"]})],
            required_tool_groups=(frozenset({"get_stock_price"}),
                                  frozenset({"send_message"})),
            max_steps=2))
    finally:
        REGISTRY["get_stock_price"] = original_stock
        REGISTRY["send_message"] = original_send

    assert sends == []
    assert client.calls == 0
    assert "didn’t prepare or send a partial update" in answer
    assert events[-1] == {"type": "done"}


def test_missing_reminder_stops_before_contact_lookup_or_draft():
    original_search = REGISTRY["search_reminders"]
    original_lookup = REGISTRY["lookup_contact"]
    lookups: list[str] = []

    async def fake_search(query: str, scope: str = "all") -> str:
        return f"Nothing active matches reminder {query!r}."

    async def fake_lookup(name: str) -> str:
        lookups.append(name)
        return "Mom: +15555550123"

    REGISTRY["search_reminders"] = Tool(
        "search_reminders", "fake reminders", {"type": "object", "properties": {
            "query": {"type": "string"}, "scope": {"type": "string"}},
            "required": ["query"]}, "assistant_read", fake_search)
    REGISTRY["lookup_contact"] = Tool(
        "lookup_contact", "fake lookup", {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
        "contacts_read", fake_lookup)
    events: list[dict] = []
    client = _NeverModelClient()
    try:
        answer = asyncio.run(loop.run_agent(
            client, "test-model", [{"role": "user", "content": VACCINE_PROMPT}],
            lambda event: _emit_to(events, event), _Approver(),
            tools=["search_reminders", "lookup_contact", "draft_message"],
            direct_calls=[
                ("search_reminders", {"query": "vaccine", "scope": "all"}),
                ("lookup_contact", {"name": "mom"}),
            ],
            required_tool_groups=(frozenset({"search_reminders"}),
                                  frozenset({"lookup_contact"}),
                                  frozenset({"draft_message"})),
            max_steps=2))
    finally:
        REGISTRY["search_reminders"] = original_search
        REGISTRY["lookup_contact"] = original_lookup

    assert lookups == []
    assert client.calls == 0
    assert answer == "Nothing active matches reminder 'vaccine'."
    assert [event["name"] for event in events if event.get("type") == "tool_call"] == [
        "search_reminders"]


def test_vaccine_prompt_uses_reminders_and_never_invents_creator():
    plan = compile_new(VACCINE_PROMPT)
    assert plan is not None
    assert plan.status == "ready"
    assert plan.sources == ["reminder"]
    assert plan.source_args == {"reminder": {"query": "vaccine", "scope": "all"}}
    assert (plan.recipient, plan.channel, plan.delivery) == ("mom", "messages", "draft")

    decision = compile_decision(plan)
    assert decision.tool_subset == [
        "search_reminders", "lookup_contact", "draft_message"]
    assert "send_message" in decision.forbidden_tools
    prompt = plan.prompt_block()
    assert "They do not identify a creator or sender" in prompt
    assert "never attribute it to Mom" in prompt


def test_reminder_search_excludes_calendar_and_disclaims_creator():
    with tempfile.TemporaryDirectory() as tmp:
        store = AssistantStore(Path(tmp) / "assistant.db")
        now = time.time()
        store.add_manual("Send vaccine report to UCSC", now - 86400)
        store.sync_source("calendar", [{
            "source_id": "calendar-vaccine", "kind": "event",
            "title": "Vaccine appointment", "when_ts": now + 86400}])
        with patch.object(assistant_tools, "assistant_store", store):
            result = asyncio.run(assistant_tools.search_reminders("vaccine", "all"))
            missing = asyncio.run(assistant_tools.search_reminders("playstation", "all"))

    assert "Send vaccine report to UCSC" in result
    assert "Vaccine appointment" not in result
    assert "creator identity is unknown" in result
    assert missing == "Nothing active matches reminder 'playstation'."


def test_grounded_workflow_can_exclude_stale_memory_context():
    client = _CaptureClient()
    with patch.object(loop.prompt_blocks, "memory_block",
                      return_value="\nPOISONED STALE MEMORY: Mom created the vaccine reminder.\n"):
        answer = asyncio.run(loop.run_agent(
            client, "test-model", [{"role": "user", "content": VACCINE_PROMPT}],
            lambda event: _emit_to([], event), _Approver(), tools=[], max_steps=1,
            include_memory_context=False))

    assert answer == "Done."
    assert client.messages is not None
    assert "POISONED STALE MEMORY" not in client.messages[0]["content"]
