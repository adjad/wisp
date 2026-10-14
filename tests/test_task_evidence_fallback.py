"""A narration step that never answers must not put raw evidence JSON on screen.

2026-10-14, "what should I do tmrow": collect_task_evidence returned epoch
seconds only, LFM2.5 (think block cannot be disabled) spent its whole
3,000-token budget converting them, produced no content, the empty-step retry
repeated the same cut-off (104s total), and the fallback showed the tool's JSON
- Messages verification codes included - as the reply.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from service.agent import loop
from service.tools import task_evidence as T
from service.tools.registry import REGISTRY, Tool

MODEL = "Agents-A1-4B-oQe6"
PDT_START = 1792047600.0  # Thu Oct 15 2026 00:00 PDT


@pytest.fixture
def pacific(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def payload() -> str:
    return json.dumps({
        "task_window": {"period": "tomorrow", "start": PDT_START, "end_exclusive": PDT_START + 86400},
        "sources": {
            "calendar": {"records": [
                {"title": "Vercel Ship SF", "when_ts": 1792080000.0, "end_ts": 1792116000.0, "all_day": 0},
                {"title": "UCSC Startup Club Meeting", "when_ts": 1792116000.0, "end_ts": 1792119600.0, "all_day": 0},
                {"title": "Week 3 Reading: Branching", "when_ts": PDT_START, "end_ts": 1792133999.0, "all_day": 1},
            ]},
            "reminders": {"records": [
                {"title": "Get doordash order", "when_ts": 1790990100.0, "temporal_state": "overdue"}]},
            "messages": {"records": [
                {"sender": "36726", "text": "Fidelity Investments: Code is: 420615"}]},
        }}, indent=2)


def test_local_time_is_the_users_wall_clock(pacific):
    assert T._local(1792116000.0) == "Thu Oct 15, 7:00 PM PDT"
    assert T._local(PDT_START, all_day=True) == "Thu Oct 15 (all day)"
    assert T._local(None) is None and T._local(float("nan")) is None
    rows = [{"when_ts": 1792080000.0, "end_ts": 1792116000.0, "all_day": 0}]
    T._add_local_times(rows)
    assert rows[0]["when_local"] == "Thu Oct 15, 9:00 AM PDT"
    assert rows[0]["end_local"] == "Thu Oct 15, 7:00 PM PDT"


def test_rendering_is_readable_and_never_quotes_bodies(pacific):
    text = T.render_evidence_text(payload())
    assert "task_window" not in text and "{" not in text
    assert "Thu Oct 15, 9:00 AM PDT to 7:00 PM PDT: Vercel Ship SF" in text
    assert "Thu Oct 15 (all day): Week 3 Reading: Branching" in text
    assert "Overdue" in text and "Get doordash order" in text
    assert "420615" not in text and "Fidelity" not in text
    assert "messages" in text  # said to be unsummarised, not silently dropped


def test_rendering_ignores_other_tools_results():
    assert T.render_evidence_text("Good afternoon - Wednesday") is None
    assert T.render_evidence_text('{"unrelated": 1}') is None
    assert T.render_evidence_text("") is None


class Approver:
    async def confirm(self, action):
        return True


async def emit(_event):
    return None


class Client:
    """Step 1 calls the tool; every later step comes back empty."""

    def __init__(self, truncated: bool):
        self.truncated = truncated
        self.calls = 0

    async def ensure_only(self, *args, **kwargs):
        return None

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"kind": "final", "message": {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "function": {"name": "collect_task_evidence", "arguments": "{}"}}]}}
            return
        message = {"role": "assistant", "content": "", "reasoning_content": "converting epochs...",
                   "tool_calls": None}
        if self.truncated:
            message["_truncated"] = True
        yield {"kind": "final", "message": message}


@pytest.fixture
def evidence_tool(pacific):
    saved = REGISTRY.get("collect_task_evidence")
    REGISTRY["collect_task_evidence"] = Tool(
        "collect_task_evidence", "evidence", {"type": "object", "properties": {}},
        "system_read", lambda: payload())
    yield
    REGISTRY["collect_task_evidence"] = saved


def run(client: Client) -> str:
    return asyncio.run(loop.run_agent(
        client, MODEL, [{"role": "user", "content": "what should I do tmrow"}],
        emit, Approver(), tools=["collect_task_evidence"], max_steps=4))


def test_truncated_narration_is_not_retried_and_shows_readable_text(evidence_tool):
    client = Client(truncated=True)
    out = run(client)
    assert client.calls == 2, "the cut-off step must not be retried with the same budget"
    assert "task_window" not in out and "420615" not in out
    assert "Vercel Ship SF" in out


def test_other_empty_steps_still_retry_and_still_render_readably(evidence_tool):
    client = Client(truncated=False)
    out = run(client)
    assert client.calls == 3, "an unparseable empty reply keeps its re-sample"
    assert "task_window" not in out and "Vercel Ship SF" in out
