"""Grammar-constrained planning (WISP_PLANNER), the planner menu, and the
per-tool read budget.

A scripted client stands in for oMLX: it returns the JSON a grammar-enforced
plan step would produce, and records every request so the tests can check what
Wisp actually sent (the response_format schema, the offered tools, whether a
narration step carried tools).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from service.agent import loop, planner
from service.tools.registry import REGISTRY, Tool

MODEL = "Agents-A1-4B-oQe6"


# ---------------------------------------------------------------- pure helpers
def test_plan_schema_enumerates_offered_names_and_requires_a_call_when_asked():
    schema = planner.plan_schema(["b_tool", "a_tool"], require_call=False)
    item = schema["properties"]["calls"]["items"]
    assert item["properties"]["tool"]["enum"] == ["a_tool", "b_tool"]
    assert schema["required"][0] == "thought"
    assert list(schema["properties"])[0] == "thought"  # reasoning precedes the choice
    assert "not_found" in schema["properties"]["status"]["enum"]
    forced = planner.plan_schema(["a_tool"], require_call=True)
    assert forced["properties"]["calls"]["minItems"] == 1
    assert forced["properties"]["status"]["enum"] == ["call_tools"]


def test_plan_schema_with_no_tools_cannot_plan_a_call():
    schema = planner.plan_schema([], require_call=False)
    assert schema["properties"]["calls"]["maxItems"] == 0
    assert "call_tools" not in schema["properties"]["status"]["enum"]


@pytest.mark.parametrize("raw,status,calls", [
    ('{"thought":"t","status":"call_tools","calls":[{"tool":"a","arguments":{"x":1}}]}',
     "call_tools", [("a", {"x": 1})]),
    ('{"thought":"t","status":"not_found","calls":[{"tool":"a","arguments":{}}]}', "not_found", []),
    ('{"thought":"t","status":"call_tools","calls":[]}', "answer", []),
    ('{"thought":"t","status":"call_tools","calls":[{"tool":"zzz","arguments":{}}]}', "answer", []),
    ('{"thought":"t","status":"call_tools","calls":[{"tool":"a","arguments":"oops"}]}',
     "call_tools", [("a", {})]),
])
def test_parse_plan(raw, status, calls):
    plan = planner.parse_plan(raw, {"a"})
    assert (plan.status, plan.calls, plan.error) == (status, calls, "")


def test_unparseable_plan_executes_nothing():
    plan = planner.parse_plan('{"thought": "cut off mid', {"a"})
    assert plan.error and plan.calls == [] and plan.ends_tool_use


def test_plan_message_is_a_native_tool_calls_message():
    msg = planner.plan_message(planner.Plan("call_tools", calls=[("a", {"q": "x"}), ("b", {})]))
    assert msg["role"] == "assistant" and len(msg["tool_calls"]) == 2
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"q": "x"}
    assert len({tc["id"] for tc in msg["tool_calls"]}) == 2


@pytest.mark.parametrize("value,expected", [(None, False), ("0", False), ("1", True), ("on", True)])
def test_planner_flag(value, expected):
    assert planner.planner_enabled(value) is expected


def test_planner_flag_rejects_garbage():
    with pytest.raises(ValueError):
        planner.planner_enabled("maybe")


def test_planner_prompt_drops_the_aggregate_collection_rule():
    offered = {"get_upcoming", "search_notes", "summarize_emails", "summarize_messages"}
    assert loop._AGGREGATE_BLOCK_PREFIX in loop.build_system(offered)
    assert loop._AGGREGATE_BLOCK_PREFIX not in loop.build_system(offered, planner=True)


# ---------------------------------------------------------------- run_agent
class PlanClient:
    """Scripted oMLX: plan steps (response_format present) return the next
    scripted plan; anything else is a narration step and answers "DONE"."""
    managed = True

    def __init__(self, plans):
        self.plans = list(plans)
        self.requests: list[dict] = []

    async def ensure_only(self, *args, **kwargs):
        return None

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        self.requests.append({"messages": list(messages), "tools": tools, **kwargs})
        if "response_format" in kwargs:
            plan = self.plans.pop(0) if self.plans else {"thought": "", "status": "answer", "calls": []}
            text = json.dumps(plan) if isinstance(plan, dict) else plan
            yield {"kind": "content", "text": text}
            yield {"kind": "final", "message": {"role": "assistant", "content": text, "tool_calls": None}}
        else:
            yield {"kind": "content", "text": "DONE"}
            yield {"kind": "final", "message": {"role": "assistant", "content": "DONE", "tool_calls": None}}


class Approver:
    async def confirm(self, action):
        return True


async def emit(_event):
    return None


@pytest.fixture(autouse=True)
def _fake_tools():
    calls: list[tuple[str, dict]] = []
    saved = {k: REGISTRY.get(k) for k in ("fake_search", "fake_read", "fake_send")}

    def search(query: str = "") -> str:
        calls.append(("fake_search", {"query": query}))
        return f"no match for {query}"

    def read() -> str:
        calls.append(("fake_read", {}))
        return "two items"
    REGISTRY["fake_search"] = Tool("fake_search", "search", {
        "type": "object", "properties": {"query": {"type": "string"}}}, "notes_read", search)
    REGISTRY["fake_read"] = Tool("fake_read", "read", {"type": "object", "properties": {}},
                                 "system_read", read)

    def send(to: str = "") -> str:
        calls.append(("fake_send", {"to": to}))
        return f"sent to {to}"
    REGISTRY["fake_send"] = Tool("fake_send", "send", {
        "type": "object", "properties": {"to": {"type": "string"}}}, "messages_send", send)
    try:
        yield calls
    finally:
        for k, v in saved.items():
            if v is None:
                REGISTRY.pop(k, None)
            else:
                REGISTRY[k] = v


def run(client, tools=("fake_search", "fake_read"), **kwargs):
    return asyncio.run(loop.run_agent(
        client, MODEL, [{"role": "user", "content": "find my gate code"}], emit, Approver(),
        tools=list(tools), **kwargs))


def plan(status="call_tools", *calls):
    return {"thought": "t", "status": status,
            "calls": [{"tool": n, "arguments": a} for n, a in calls]}


def test_planned_calls_run_then_status_answer_narrates_without_tools(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_read", {})), plan("answer")])
    assert run(client, planner=True) == "DONE"
    assert _fake_tools == [("fake_read", {})]
    first = client.requests[0]
    enum = first["response_format"]["json_schema"]["schema"]["properties"]["calls"]["items"][
        "properties"]["tool"]["enum"]
    assert enum == ["fake_read", "fake_search"]
    assert first["messages"][-1]["role"] == "system" and "PLANNING STEP" in first["messages"][-1]["content"]
    narration = client.requests[-1]
    assert "response_format" not in narration and not narration["tools"]
    assert narration["messages"][-1]["content"] == planner.STATUS_NOTES["answer"]


def test_not_found_status_reaches_the_narration_step(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_search", {"query": "gate code"})), plan("not_found")])
    run(client, planner=True)
    assert client.requests[-1]["messages"][-1]["content"] == planner.STATUS_NOTES["not_found"]


def test_read_budget_stops_the_third_rephrasing(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_search", {"query": q})) for q in ("gate code", "gate", "code")]
                        + [plan("not_found")])
    run(client, planner=True)
    assert [a["query"] for _, a in _fake_tools] == ["gate code", "gate"]
    budget_result = next(m["content"] for m in client.requests[3]["messages"]
                         if m.get("role") == "tool" and "NOT run again" in m["content"])
    assert "fake_search has already run 2 times" in budget_result


def test_a_plan_mixing_reads_and_effects_runs_only_the_reads(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_read", {}), ("fake_send", {"to": "<contact>"})),
                         plan("call_tools", ("fake_send", {"to": "+15550100"})), plan("answer")])
    run(client, tools=("fake_read", "fake_send"), planner=True)
    assert _fake_tools == [("fake_read", {}), ("fake_send", {"to": "+15550100"})]


def test_unparseable_plan_is_resampled_not_executed(_fake_tools):
    client = PlanClient(['{"thought": "cut off', plan("call_tools", ("fake_read", {})), plan("answer")])
    assert run(client, planner=True) == "DONE"
    assert _fake_tools == [("fake_read", {})]


def test_without_the_flag_no_plan_step_is_sent(_fake_tools):
    client = PlanClient([])
    run(client)
    assert all("response_format" not in r for r in client.requests)


def test_unmanaged_client_never_plans(_fake_tools):
    client = PlanClient([])
    client.managed = False
    run(client, planner=True)
    assert all("response_format" not in r for r in client.requests)


# ---------------------------------------------------------------- menu
@pytest.mark.parametrize("prompt,must", [
    ("do i have anything on sunday", "get_upcoming"),          # router offered stopwatch, not this
    ("let dad know i landed", "send_message"),                 # router offered forward_email only
    ("remember that my wife's birthday is march 3", "remember"),  # fs_write category hid it
    ("find my resume and email it to dana@example.com", "find_files"),
    ("find my resume and email it to dana@example.com", "send_email"),
])
def test_menu_covers_the_measured_router_misses(prompt, must):
    from service.router.menu import build_menu
    assert must in build_menu(prompt)


def test_a_hinted_request_does_not_import_unrelated_families():
    from service.router.menu import build_menu
    menu = build_menu("whats on my todo list")
    assert {"get_upcoming", "search_reminders"} <= set(menu)
    assert not {"wisp_mcp", "set_display", "list_bluetooth_devices"} & set(menu)


def test_menu_respects_forbidden_and_cap():
    from service.router.menu import build_menu, MAX_MENU
    menu = build_menu("text mom and email dad and check my calendar and notes",
                      forbidden=frozenset({"send_message"}))
    assert "send_message" not in menu and len(menu) <= MAX_MENU


def test_menu_offers_sends_only_with_an_outbound_cue():
    # The outbound gate (router/semantic.outbound_intent) fails toward True
    # once a person is named, so "what did mom say" may carry send tools; the
    # policy engine still confirms every send. With no person and no send
    # verb the gate holds.
    from service.router.menu import build_menu
    assert "send_message" not in build_menu("summarize my messages from today")
    assert "send_message" in build_menu("text sam that i'm running late")
