"""Agent-loop enforcement for conditional actions and outbound grounding.

    .venv/bin/python tests/test_execution_contract_loop.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402
from service.tools.registry import REGISTRY, Tool  # noqa: E402

PASS = FAIL = 0
STATE = {"toggle": 0, "send": 0, "battery": 80}


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


class Client:
    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    async def ensure_only(self, *args, **kwargs):
        return None

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        item = self.script[self.i] if self.i < len(self.script) else None
        self.i += 1
        if item:
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


def install_fakes():
    def battery():
        return f"charge: {STATE['battery']}%, on battery power"

    def toggle(setting: str, on: bool):
        STATE["toggle"] += 1
        return f"{setting} {'enabled' if on else 'disabled'}"

    def read_price():
        return "NVIDIA start $9.00; latest $10.00"

    def send(to: str, text: str):
        STATE["send"] += 1
        return f"Message sent to {to}."

    REGISTRY["get_battery_status"] = Tool(
        "get_battery_status", "fake battery", {"type": "object", "properties": {}},
        "system_read", battery)
    REGISTRY["toggle_setting"] = Tool(
        "toggle_setting", "fake setting", {"type": "object", "properties": {
            "setting": {"type": "string"}, "on": {"type": "boolean"}},
            "required": ["setting", "on"]}, "system_write", toggle)
    REGISTRY["fake_price"] = Tool(
        "fake_price", "fake source", {"type": "object", "properties": {}},
        "web_read", read_price)
    REGISTRY["send_message"] = Tool(
        "send_message", "fake send", {"type": "object", "properties": {
            "to": {"type": "string"}, "text": {"type": "string"}},
            "required": ["to", "text"]}, "messages_send", send)


def run(script, **kwargs):
    return asyncio.run(loop.run_agent(
        Client(script), "Agents-A1-4B-oQe6",
        [{"role": "user", "content": kwargs.pop("prompt", "do it")}],
        emit, Approver(), max_steps=kwargs.pop("max_steps", 5), **kwargs))


def test_conditional_waiver_and_execution() -> None:
    print("\nconditional actions are evaluated from successful source results")
    base = dict(
        tools=["get_battery_status", "toggle_setting"],
        required_tool_groups=(frozenset({"get_battery_status"}),
                              frozenset({"toggle_setting"})),
        conditional_tools=(("get_battery_status", "toggle_setting", "percent_below", 20),),
    )
    STATE.update(toggle=0, battery=80)
    run([("get_battery_status", {}), None], **base)
    check("high battery waives the write", STATE["toggle"] == 0)

    STATE.update(toggle=0, battery=10)
    run([("get_battery_status", {}), None,
         ("toggle_setting", {"setting": "low_power_mode", "on": True}), None], **base)
    check("low battery executes the confirmed write once", STATE["toggle"] == 1)


def test_outbound_facts_are_grounded() -> None:
    print("\noutbound numeric facts must exist in prompt or source evidence")
    contract = dict(
        tools=["fake_price", "send_message"], max_steps=2,
        required_tool_groups=(frozenset({"fake_price"}), frozenset({"send_message"})),
        prompt="Send Mom the price after looking it up.")

    STATE["send"] = 0
    run([("fake_price", {}), ("send_message", {"to": "Mom", "text": "Price: $99.00"})],
        **contract)
    check("an invented price is blocked before confirmation", STATE["send"] == 0)

    STATE["send"] = 0
    run([("fake_price", {}), ("send_message", {"to": "Mom", "text": "Price: $10.00"})],
        **contract)
    check("a sourced price may reach the confirmed tool", STATE["send"] == 1)


if __name__ == "__main__":
    install_fakes()
    test_conditional_waiver_and_execution()
    test_outbound_facts_are_grounded()
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
