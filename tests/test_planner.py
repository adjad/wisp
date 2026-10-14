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
    assert schema["required"] == ["thought", "calls", "status"]
    assert list(schema["properties"]) == ["thought", "calls", "status"]  # reason, commit, then label
    assert "not_found" in schema["properties"]["status"]["enum"]
    forced = planner.plan_schema(["a_tool"], require_call=True)
    assert forced["properties"]["calls"]["minItems"] == 1
    assert forced["properties"]["status"]["enum"] == ["call_tools"]


def test_not_found_is_unavailable_before_anything_was_searched():
    assert "not_found" not in planner.plan_schema(["a"], require_call=False, searched=False)[
        "properties"]["status"]["enum"]


def test_first_plan_step_cannot_claim_not_found(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_search", {"query": "gate code"})), plan("not_found")])
    run(client, planner=True)
    enums = [r["response_format"]["json_schema"]["schema"]["properties"]["status"]["enum"]
             for r in client.requests if "response_format" in r]
    assert "not_found" not in enums[0] and "not_found" in enums[1]


def test_plan_schema_with_no_tools_cannot_plan_a_call():
    schema = planner.plan_schema([], require_call=False)
    assert schema["properties"]["calls"]["maxItems"] == 0
    assert "call_tools" not in schema["properties"]["status"]["enum"]


@pytest.mark.parametrize("raw,status,calls", [
    ('{"thought":"t","status":"call_tools","calls":[{"tool":"a","arguments":{"x":1}}]}',
     "call_tools", [("a", {"x": 1})]),
    # calls are written before status; listed calls win
    ('{"thought":"t","calls":[{"tool":"a","arguments":{}}],"status":"not_found"}', "call_tools", [("a", {})]),
    ('{"thought":"t","calls":[],"status":"not_found"}', "not_found", []),
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
    saved = {k: REGISTRY.get(k) for k in ("fake_search", "fake_read", "fake_send", "fake_open", "fake_write")}

    def search(query: str = "") -> str:
        calls.append(("fake_search", {"query": query}))
        return f"(no notes matched {query})"

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
    def open_(path: str = "") -> str:
        calls.append(("fake_open", {"path": path}))
        return f"contents of {path}"
    REGISTRY["fake_open"] = Tool("fake_open", "read a file", {
        "type": "object", "properties": {"path": {"type": "string"}}}, "fs_read", open_)

    def write(path: str = "", text: str = "") -> str:
        calls.append(("fake_write", {"path": path, "text": text}))
        return f"wrote {path}"
    REGISTRY["fake_write"] = Tool("fake_write", "write a file", {
        "type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}}},
        "fs_write", write)
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
    # The exhausted third search is dropped at the plan step, which reports
    # not_found instead of sending the model a refusal pointer.
    assert client.requests[-1]["messages"][-1]["content"] == planner.STATUS_NOTES["not_found"]


def test_a_plan_mixing_reads_and_effects_runs_only_the_reads(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_read", {}), ("fake_send", {"to": "<contact>"})),
                         plan("call_tools", ("fake_send", {"to": "+15550100"})), plan("answer")])
    run(client, tools=("fake_read", "fake_send"), planner=True)
    assert _fake_tools == [("fake_read", {}), ("fake_send", {"to": "+15550100"})]


def test_replanning_a_call_that_already_ran_answers_instead(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_read", {})), plan("call_tools", ("fake_read", {}))])
    assert run(client, planner=True) == "DONE"
    assert _fake_tools == [("fake_read", {})]
    narration = client.requests[-1]
    assert narration["messages"][-1]["content"] == planner.STATUS_NOTES["answer"]
    assert not any("already ran" in str(m.get("content")) for m in narration["messages"])


def test_cannot_do_before_any_search_replans_with_a_call_required(_fake_tools):
    client = PlanClient([plan("cannot_do"), plan("call_tools", ("fake_read", {})), plan("answer")])
    assert run(client, planner=True) == "DONE"
    assert _fake_tools == [("fake_read", {})]
    second = client.requests[1]["response_format"]["json_schema"]["schema"]
    assert second["properties"]["calls"]["minItems"] == 1


def test_cannot_do_after_a_search_is_honoured(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_read", {})), plan("cannot_do")])
    run(client, planner=True)
    assert client.requests[-1]["messages"][-1]["content"] == planner.STATUS_NOTES["cannot_do"]


def test_narration_that_writes_a_tool_call_triggers_one_replan(_fake_tools):
    class Leaky(PlanClient):
        narrations = 0

        async def stream_events(self, model, messages, *, tools=None, **kwargs):
            if "response_format" not in kwargs and self.narrations == 0:
                self.narrations += 1
                self.requests.append({"messages": list(messages), "tools": tools, **kwargs})
                text = "<|tool_call_start|>[fake_read()]<|tool_call_end|>"
                yield {"kind": "final", "message": {"role": "assistant", "content": text, "tool_calls": None}}
                return
            async for ev in super().stream_events(model, messages, tools=tools, **kwargs):
                yield ev
    client = Leaky([plan("answer"), plan("call_tools", ("fake_read", {})), plan("answer")])
    assert run(client, planner=True) == "DONE"
    assert _fake_tools == [("fake_read", {})]


@pytest.mark.parametrize("raw,clean", [
    ("<|tool_call_start|>[get_upcoming()]<|tool_call_end|>", ""),
    ("Let me check. <|tool_call_start|>[get_upcoming()]<|tool_call_end|>", "Let me check."),
    ("Sure <tool_call>search_notes<arg_key>q</arg_key>", "Sure"),
])
def test_strip_tool_call_markup(raw, clean):
    assert planner.strip_tool_call_markup(raw) == clean


def test_markup_after_the_replan_is_spent_never_reaches_the_user(_fake_tools):
    class AlwaysLeaky(PlanClient):
        async def stream_events(self, model, messages, *, tools=None, **kwargs):
            if "response_format" in kwargs:
                async for ev in super().stream_events(model, messages, tools=tools, **kwargs):
                    yield ev
                return
            self.requests.append({"messages": list(messages), "tools": tools, **kwargs})
            text = "<|tool_call_start|>[fake_read()]<|tool_call_end|>"
            yield {"kind": "final", "message": {"role": "assistant", "content": text, "tool_calls": None}}
    out = run(AlwaysLeaky([plan("answer"), plan("call_tools", ("fake_read", {})), plan("answer")]),
              planner=True)
    assert "tool_call" not in out and out.strip()


def test_only_one_forced_replan_per_turn(_fake_tools):
    client = PlanClient([plan("cannot_do"), plan("call_tools", ("fake_read", {})), plan("cannot_do")])
    run(client, planner=True)
    plan_steps = [r for r in client.requests if "response_format" in r]
    assert len(plan_steps) == 3
    assert client.requests[-1]["messages"][-1]["content"] == planner.STATUS_NOTES["cannot_do"]


# ---- independent review of #211 (BLOCK at 5abefc78): regressions
def test_a_repeated_read_never_takes_a_fresh_write_down_with_it(_fake_tools):
    # finding 2: deferral ran before repeat-dropping, so the write vanished.
    client = PlanClient([plan("call_tools", ("fake_open", {"path": "a"})),
                         plan("call_tools", ("fake_open", {"path": "a"}), ("fake_write", {"path": "a", "text": "new"})),
                         plan("answer")])
    run(client, tools=("fake_open", "fake_write"), planner=True)
    assert _fake_tools == [("fake_open", {"path": "a"}), ("fake_write", {"path": "a", "text": "new"})]


def test_a_read_after_a_write_runs_again(_fake_tools):
    # finding 3: a write invalidates completed reads, so read-write-read re-reads.
    client = PlanClient([plan("call_tools", ("fake_open", {"path": "a"})),
                         plan("call_tools", ("fake_write", {"path": "a", "text": "new"})),
                         plan("call_tools", ("fake_open", {"path": "a"})), plan("answer")])
    run(client, tools=("fake_open", "fake_write"), planner=True)
    assert [c for c in _fake_tools if c[0] == "fake_open"] == [("fake_open", {"path": "a"})] * 2


def test_a_failed_read_may_be_retried_identically(_fake_tools):
    # finding 3: only SUCCESSFUL reads count as already answered.
    attempts = []

    def flaky(path: str = "") -> str:
        attempts.append(path)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return f"contents of {path}"
    REGISTRY["fake_open"] = Tool("fake_open", "read a file", {
        "type": "object", "properties": {"path": {"type": "string"}}}, "fs_read", flaky)
    client = PlanClient([plan("call_tools", ("fake_open", {"path": "a"})),
                         plan("call_tools", ("fake_open", {"path": "a"})), plan("answer")])
    run(client, tools=("fake_open",), planner=True)
    assert attempts == ["a", "a"]


def test_reading_three_named_files_is_not_budgeted(_fake_tools):
    # finding 4: distinct targets are separate work, not search rephrasing.
    client = PlanClient([plan("call_tools", ("fake_open", {"path": p})) for p in ("a", "b", "c")]
                        + [plan("answer")])
    run(client, tools=("fake_open",), planner=True)
    assert [a["path"] for _, a in _fake_tools] == ["a", "b", "c"]


@pytest.mark.parametrize("prompt", [
    "what is my gate code", "what's my wife's birthday", "what was my gate code before the reset",
    "delete the old file and recall my gate code", "do not forget my gate code",
    "don't forget my gate code", "don\u2019t forget my gate code", "do not stop remembering my gate code",
    "do not unlearn my gate code", "do not ever again forget my gate code",
    "did you forget my gate code?", "why did you forget my gate code", "i forget my gate code; what is it?",
    "delete my notes, but keep your memory of my gate code",
    "delete the old file, not my memory of the gate code",
    "remove the meeting and keep it in memory"])
def test_forget_is_not_offered_without_a_request_to_forget(prompt):
    from service.router.menu import build_menu
    assert "forget" not in build_menu(prompt)
    assert "forget" not in build_menu("what is my gate code",
                                      last_assistant="Would you like me to send a message?")


@pytest.mark.parametrize("prompt", [
    "forget my gate code", "stop remembering my gate code", "remove my gate code from memory",
    "forget that my wife's birthday is march 3", "please forget my old address",
    "can you delete what you know about my ex", "delete your memory of my gate code",
    "remove my old address from your memory", "clear all of your memories"])
def test_forget_is_offered_for_a_request_to_forget(prompt):
    from service.router.menu import build_menu
    assert "forget" in build_menu(prompt)


def test_three_successful_searches_are_not_budgeted(_fake_tools):
    def hit(query: str = "") -> str:
        _fake_tools.append(("fake_search", {"query": query}))
        return f"note about {query}"
    REGISTRY["fake_search"] = Tool("fake_search", "search", {
        "type": "object", "properties": {"query": {"type": "string"}}}, "notes_read", hit)
    client = PlanClient([plan("call_tools", ("fake_search", {"query": q})) for q in ("alpha", "beta", "gamma")]
                        + [plan("answer")])
    run(client, planner=True)
    assert [a["query"] for _, a in _fake_tools] == ["alpha", "beta", "gamma"]


def test_misses_are_budgeted_for_reads_without_a_query_param(_fake_tools):
    def miss(path: str = "") -> str:
        _fake_tools.append(("fake_open", {"path": path}))
        return f"(no file named {path})"
    REGISTRY["fake_open"] = Tool("fake_open", "read a file", {
        "type": "object", "properties": {"path": {"type": "string"}}}, "fs_read", miss)
    client = PlanClient([plan("call_tools", ("fake_open", {"path": p})) for p in ("Sam", "Samuel", "Sammy")]
                        + [plan("not_found")])
    run(client, tools=("fake_open",), planner=True)
    assert [a["path"] for _, a in _fake_tools] == ["Sam", "Samuel"]


def test_a_binding_that_makes_a_read_a_repeat_keeps_the_write(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_open", {"path": "a"})),
                         plan("call_tools", ("fake_open", {"path": "different"}),
                              ("fake_write", {"path": "a", "text": "new"})),
                         plan("answer")])
    run(client, tools=("fake_open", "fake_write"), planner=True,
        tool_argument_bindings={"fake_open": {"path": "a"}})
    assert _fake_tools == [("fake_open", {"path": "a"}), ("fake_write", {"path": "a", "text": "new"})]


@pytest.mark.parametrize("text", ["", "   ", "No matching memory or user conversation passage.",
                                  "No results", "(no notes matched x)", "nothing found", "Couldn't find it",
                                  "No public web results found."])
def test_empty_results_count_as_misses(text):
    assert planner.looks_empty(text)


@pytest.mark.parametrize("text", ["note about alpha", "Notes: gate code 4512", "Nobody emailed"])
def test_real_results_are_not_misses(text):
    assert not planner.looks_empty(text)


def test_an_exhausted_read_never_takes_a_fresh_write_down_with_it(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_search", {"query": "a"})),
                         plan("call_tools", ("fake_search", {"query": "b"})),
                         plan("call_tools", ("fake_search", {"query": "c"}),
                              ("fake_write", {"path": "status", "text": "done"})),
                         plan("answer")])
    run(client, tools=("fake_search", "fake_write"), planner=True)
    assert [c[0] for c in _fake_tools] == ["fake_search", "fake_search", "fake_write"]


def test_remember_is_offered_for_a_request_to_remember():
    from service.router.menu import build_menu
    assert "remember" in build_menu("remember that my gate code is 4512")


def test_planned_calls_still_get_argument_bindings(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_send", {"to": "+15559999"})), plan("answer")])
    run(client, tools=("fake_send",), planner=True,
        tool_argument_bindings={"fake_send": {"to": "+15550100"}})
    assert _fake_tools == [("fake_send", {"to": "+15550100"})]


def test_a_forbidden_tool_is_not_plannable(_fake_tools):
    client = PlanClient([plan("call_tools", ("fake_send", {"to": "x"})), plan("answer")])
    run(client, tools=("fake_read", "fake_send"), planner=True,
        forbidden_tools=frozenset({"fake_send"}))
    enum = client.requests[0]["response_format"]["json_schema"]["schema"]["properties"]["calls"][
        "items"]["properties"]["tool"]["enum"]
    assert "fake_send" not in enum and _fake_tools == []


def test_denied_effect_in_a_plan_does_not_run(_fake_tools):
    class Deny:
        async def confirm(self, action):
            return False
    client = PlanClient([plan("call_tools", ("fake_send", {"to": "+15550100"})), plan("answer")])
    asyncio.run(loop.run_agent(client, MODEL, [{"role": "user", "content": "text sam hi"}], emit,
                               Deny(), tools=["fake_send"], planner=True))
    assert _fake_tools == []


def test_flag_off_requests_carry_nothing_from_the_planner(_fake_tools):
    offered = {"fake_read", "fake_search"}
    assert loop.build_system(offered) == loop.build_system(offered, planner=False)
    client = PlanClient([])
    run(client)
    for r in client.requests:
        assert "response_format" not in r
        assert not any("PLANNING STEP" in str(m.get("content")) for m in r["messages"])


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


@pytest.mark.parametrize("prompt", ["holler at Sam", "ask Sam if he's free for dinner", "give dad a ring"])
def test_a_request_addressed_to_someone_offers_messaging(prompt):
    from service.router.menu import build_menu
    menu = build_menu(prompt)
    assert {"send_message", "draft_message"} & set(menu)


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
