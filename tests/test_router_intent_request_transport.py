"""Mocked wire contracts. Pending client ownership is preserved as strict xfails."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from service.config.endpoints import Endpoint, Target
from service.inference.omlx_client import OMLXClient
from service.router.intent import planner
from service.router.intent.request import build_messages, completion_options
from service.router.intent.schema import SCHEMA

MODEL = "Ling-3.0-tiny-oQ6e"
TARGET = Target("router", Endpoint("local", "http://127.0.0.1:8000", "local_omlx", True), MODEL)
NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
CONFIG = {"enabled": True, "domains": ["calendar", "reminders", "email", "messages", "notes"]}
ANSWER = {"version": 1, "kind": "read", "sources": [
    {"domain": "email", "operation": "overview"}], "excluded_sources": [], "unsupported_constraints": []}


def run_plan(monkeypatch, *, window=8000, prompt="Recap my email", history=(), responses=None):
    target = replace(TARGET, context_window=window)
    monkeypatch.setattr(planner, "role_target", lambda role: target)
    captured = []

    async def run():
        client = OMLXClient(target=target, api_key="synthetic-fixture")
        await client._client.aclose()

        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps((responses or [ANSWER])[min(len(captured)-1, len(responses or [ANSWER])-1)])}}]})

        client._client = httpx.AsyncClient(base_url=target.endpoint.base_url,
                                          transport=httpx.MockTransport(handler))
        client.status = AsyncMock(return_value={"models": [{"id": MODEL, "loaded": True}]})
        try:
            result = await planner.plan_read(prompt, client=client, config=CONFIG,
                                             now=NOW, context=history)
            return result, captured
        finally:
            await client.aclose()
    return asyncio.run(run())


def test_planner_wire_prefix_matches_shared_builder(monkeypatch):
    history = [{"role": "user", "content": "My email"},
               {"role": "assistant", "content": "Email overview. [Tools: summarize_emails]"}]
    result, bodies = run_plan(monkeypatch, history=history)
    assert result.disposition == "compiled"
    assert bodies[0]["messages"] == build_messages("Recap my email", context=history, now=NOW)
    assert bodies[0]["response_format"] == completion_options(SCHEMA)["response_format"]
    assert bodies[0]["temperature"] == 0
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.xfail(strict=True, reason="PA-BUDGET-01: shared client fix awaits retained performance-owner baton")
def test_bound_router_wire_preserves_requested_900_tokens(monkeypatch):
    result, bodies = run_plan(monkeypatch)
    assert result.disposition == "compiled"
    assert bodies[0]["max_tokens"] == 900


@pytest.mark.xfail(strict=True, reason="PA-BUDGET-01: 2000 floor incorrectly rejects a small structured request")
def test_small_context_admits_routing_request_without_budget_growth(monkeypatch):
    result, bodies = run_plan(monkeypatch, window=2400)
    assert result.disposition == "compiled"
    assert len(bodies) == 1
    assert bodies[0]["max_tokens"] == 900


def test_context_overflow_clarifies_without_wire_or_source_calls(monkeypatch):
    result, bodies = run_plan(monkeypatch, window=512)
    assert result.disposition == "clarify"
    assert not result.calls and not bodies


@pytest.mark.parametrize("window", [4000, 8000])
def test_history_pressure_keeps_every_selected_message_or_rejects(monkeypatch, window):
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "h" * 2000}
               for i in range(6)]
    expected = build_messages("Recap my email", context=history, now=NOW)
    result, bodies = run_plan(monkeypatch, window=window, history=history)
    if bodies:
        assert all(body["messages"] == expected for body in bodies)
    else:
        assert result.disposition == "clarify" and not result.calls


@pytest.mark.parametrize("window", [4000, 8000])
def test_repair_keeps_original_request_and_history_or_rejects(monkeypatch, window):
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "h" * 2000}
               for i in range(6)]
    expected = build_messages("Recap my email", context=history, now=NOW)
    result, bodies = run_plan(monkeypatch, window=window, history=history,
                              responses=[{"invalid": True}, ANSWER])
    if bodies:
        assert len(bodies) == 2
        assert bodies[0]["messages"] == expected
        if len(bodies) > 1:
            assert bodies[1]["messages"][:-1] == expected
            assert bodies[1]["messages"][-1]["role"] == "user"
            assert "original latest request" in bodies[1]["messages"][-1]["content"]
    else:
        assert result.disposition == "clarify" and not result.calls


@pytest.mark.xfail(strict=True, reason="PA-TRANSPORT-COVERAGE-04: response_format overhead is omitted")
def test_schema_pressure_rejects_before_wire(monkeypatch):
    schema = deepcopy(SCHEMA)
    schema["description"] = "synthetic schema overhead " * 2000
    monkeypatch.setattr(planner, "SCHEMA", schema)
    result, bodies = run_plan(monkeypatch, window=8000)
    assert result.disposition == "clarify"
    assert not result.calls and not bodies
