"""The executor must enforce workflow-owned arguments on model tool calls."""
from __future__ import annotations

import asyncio
import json

from service.agent import loop
from service.tools.registry import REGISTRY, Tool


class _Client:
    def __init__(self):
        self.calls = 0

    async def ensure_only(self, model, **kwargs):
        return None

    async def stream_events(self, model, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"kind": "final", "message": {
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {
                        "name": "fake_bound_delivery",
                        "arguments": json.dumps({"to": "Wrong Person", "body": "hello"}),
                    },
                }],
            }}
        else:
            yield {"kind": "content", "text": "Done."}
            yield {"kind": "final", "message": {
                "role": "assistant", "content": "Done.", "tool_calls": None,
            }}


class _Approver:
    async def confirm(self, action):
        return True


def test_workflow_binding_overrides_model_recipient_before_execution():
    observed = []

    def fake_bound_delivery(to: str, body: str = ""):
        observed.append({"to": to, "body": body})
        return f"delivered to {to}"

    REGISTRY["fake_bound_delivery"] = Tool(
        name="fake_bound_delivery",
        description="test-only delivery",
        parameters={"type": "object", "properties": {
            "to": {"type": "string"}, "body": {"type": "string"},
        }, "required": ["to", "body"]},
        category="system_read",
        func=fake_bound_delivery,
    )
    events = []

    async def emit(event):
        events.append(event)

    try:
        asyncio.run(loop.run_agent(
            _Client(), "test-model", [{"role": "user", "content": "deliver it"}],
            emit, _Approver(), tools=["fake_bound_delivery"], max_steps=3,
            expect_tool_first=True,
            tool_argument_bindings={"fake_bound_delivery": {"to": "Mom"}},
        ))
    finally:
        REGISTRY.pop("fake_bound_delivery", None)

    assert observed == [{"to": "Mom", "body": "hello"}]
    call = next(event for event in events if event["type"] == "tool_call")
    assert call["args"]["to"] == "Mom"
