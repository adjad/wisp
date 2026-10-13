"""A step made only of refused repeats is followed by a tool_choice="none" step.

The repeated-read guard returns a pointer instead of re-running an identical
read, but a small model can keep asking anyway: measured 2026-10-13 on
Ling-3.0-tiny-Wisp-V3-merged-oQ4e, "What do I need to do today?" re-called
the same tool on seven further steps (34.9s) until max_steps. The loop now
treats "every call last step was refused" as the end of fetching: the next
step may not call a tool, so the model answers from what it already has.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from service.agent import loop
from service.tools.registry import REGISTRY, Tool

MODEL = "Agents-A1-4B-oQe6"


class Client:
    """Scripted model: each entry is a tool call to make; honours
    tool_choice="none" by answering with text instead, like a real model."""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0
        self.choices: list[str | None] = []

    async def ensure_only(self, *args, **kwargs):
        return None

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        choice = kwargs.get("tool_choice")
        self.choices.append(choice)
        self.last_messages = list(messages)
        item = self.script[self.i] if self.i < len(self.script) else None
        self.i += 1
        if item and choice != "none":
            name, args = item
            yield {"kind": "final", "message": {
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": f"c{self.i}",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }]}}
        else:
            yield {"kind": "content", "text": "DONE"}
            yield {"kind": "final", "message": {
                "role": "assistant", "content": "DONE", "tool_calls": None}}


class Approver:
    async def confirm(self, action):
        return True


async def emit(_event):
    return None


@pytest.fixture(autouse=True)
def _fake_tools():
    saved = {k: REGISTRY.get(k) for k in ("fake_evidence", "fake_other")}
    REGISTRY["fake_evidence"] = Tool(
        "fake_evidence", "fake read", {"type": "object", "properties": {}},
        "system_read", lambda: "evidence: three things to do")
    REGISTRY["fake_other"] = Tool(
        "fake_other", "fake read two", {"type": "object", "properties": {}},
        "system_read", lambda: "other: nothing")
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                REGISTRY.pop(k, None)
            else:
                REGISTRY[k] = v


def run(client: Client, **kwargs) -> str:
    return asyncio.run(loop.run_agent(
        client, MODEL, [{"role": "user", "content": "what do I need to do today?"}],
        emit, Approver(), max_steps=kwargs.pop("max_steps", 8), **kwargs))


def test_repeat_only_step_forces_a_narration_step():
    client = Client([("fake_evidence", {})] * 8)
    out = run(client, tools=["fake_evidence"])
    # step 1 runs the read, step 2 is refused as a repeat, step 3 may not call.
    assert client.choices[:3] == ["auto", "auto", "none"]
    assert len(client.choices) == 3, client.choices
    assert out == "DONE"
    # The stalled step also carries the narration nudge, right after the
    # refusal pointer — V3 produced nothing from the choice alone.
    tail = client.last_messages[-2:]
    assert tail[0]["role"] == "tool" and "already ran" in tail[0]["content"]
    assert tail[1] == {"role": "user", "content": loop._NARRATE_NUDGE}


def test_a_new_call_alongside_a_repeat_keeps_choice_open():
    # Step 2 repeats the read AND asks for a different tool: not a stall.
    class Mixed(Client):
        async def stream_events(self, model, messages, *, tools=None, **kwargs):
            self.choices.append(kwargs.get("tool_choice"))
            self.last_messages = list(messages)
            self.i += 1
            if self.i == 1:
                calls = [("fake_evidence", {})]
            elif self.i == 2:
                calls = [("fake_evidence", {}), ("fake_other", {})]
            else:
                yield {"kind": "content", "text": "DONE"}
                yield {"kind": "final", "message": {"role": "assistant", "content": "DONE", "tool_calls": None}}
                return
            yield {"kind": "final", "message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{self.i}{j}", "function": {"name": n, "arguments": json.dumps(a)}}
                for j, (n, a) in enumerate(calls)]}}

    client = Mixed([])
    run(client, tools=["fake_evidence", "fake_other"])
    assert client.choices[2] == "auto"
    assert all(m.get("content") != loop._NARRATE_NUDGE for m in client.last_messages)


def test_unmet_required_group_keeps_choice_open():
    # The route still requires fake_other; a refused repeat of fake_evidence
    # must not stop the model from calling it.
    client = Client([("fake_evidence", {}), ("fake_evidence", {}), ("fake_other", {})])
    run(client, tools=["fake_evidence", "fake_other"],
        required_tool_groups=(frozenset({"fake_evidence"}), frozenset({"fake_other"})),
        multi_round=True)
    assert "none" not in client.choices[:3]
