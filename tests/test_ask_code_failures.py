"""Wisp-side causes of Ask toolbench failures (baseline 20261031-011715, 58.0%).

Each test names the toolbench case(s) whose failure was the host's, not the
model's. Synthetic tools, scripted clients and isolated stores only.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-ask-code-"))

import pytest  # noqa: E402

from service.tools import registry  # noqa: E402
from tests.test_model_led_integration import Approval, call, run  # noqa: E402,F401
from tests.test_model_led_integration import synthetic  # noqa: E402,F401  (fixture)


def receipts(request):
    return [json.loads(m["content"]) for m in request["messages"]
            if m.get("role") == "tool" and m["content"].startswith("{")]


def tool_results(request):
    return [m["content"] for m in request["messages"] if m.get("role") == "tool"]


# --- stubs and unoffered calls ----------------------------------------------

def _tool(name, category="assistant_read", parameters=None):
    async def fn(**_):
        return "ok"
    return registry.Tool(name, "d", parameters or {"type": "object", "properties": {
        "query": {"type": "string"}}, "required": []}, category, fn)


def test_runnable_stub_call_needs_arguments_that_fit_the_real_schema():
    read = _tool("read_x")
    assert registry.runnable_stub_call(read, {})
    assert registry.runnable_stub_call(read, {"query": "dentist"})
    assert not registry.runnable_stub_call(read, {"q": "dentist"})          # unknown name
    assert not registry.runnable_stub_call(read, {"query": 3})              # wrong type
    write = _tool("write_x", "fs_write", {"type": "object", "properties": {
        "path": {"type": "string"}}, "required": ["path"]})
    assert not registry.runnable_stub_call(write, {})                       # missing required
    assert registry.runnable_stub_call(write, {"path": "/tmp/a"})
    bare_effect = _tool("open_x", "app_control", {"type": "object", "properties": {}})
    assert not registry.runnable_stub_call(bare_effect, {})                 # an effect needs arguments
    assert not registry.runnable_stub_call(None, {})


def test_nested_parameters_wrapper_is_unwrapped_only_when_it_is_not_an_argument():
    # web_latch_bitcoin_first_lc: view_emails({"parameters": {"query": "Chase"}}).
    read = _tool("read_x")
    assert registry.unwrap_nested_args(read, {"parameters": {"query": "Chase"}}) == {"query": "Chase"}
    assert registry.unwrap_nested_args(read, {"arguments": {"query": "Chase"}}) == {"query": "Chase"}
    assert registry.unwrap_nested_args(read, {"parameters": {"query": "a"}, "x": 1}) == {
        "parameters": {"query": "a"}, "x": 1}
    own = _tool("own_x", parameters={"type": "object", "properties": {"parameters": {"type": "object"}}})
    assert registry.unwrap_nested_args(own, {"parameters": {"a": 1}}) == {"parameters": {"a": 1}}


def test_namespaced_name_of_a_registered_tool_is_that_tool(synthetic):
    # mem_forget_allergy / mem_forget_work_count: "memory.forget", "memory:forget".
    from service.agent.loop import _clean_tool_name
    assert _clean_tool_name("calendar.get_upcoming") == "get_upcoming"
    assert _clean_tool_name("calendar:get_upcoming") == "get_upcoming"
    assert _clean_tool_name("functions.view_emails") == "view_emails"
    assert _clean_tool_name("memory.no_such_tool") == "memory.no_such_tool"


def test_unoffered_admitted_tool_loads_its_schema_and_never_runs(synthetic):
    # fu_tell_him_yes: send_message called while only draft_message was listed.
    effects, _ = synthetic
    client, _, _ = run([[call("write_file", path="/tmp/x.txt", content="hi")], "Synthetic answer."],
                       ask_quality=True, model_led_stub_rank=[])
    first = {s["function"]["name"] for s in client.requests[0]["tools"]}
    assert "write_file" not in first                    # neither a schema nor a stub
    assert effects == []
    [receipt] = receipts(client.requests[1])
    assert receipt["not_run_yet"] == ["write_file"]
    assert "description" in next(s for s in client.requests[1]["tools"]
                                 if s["function"]["name"] == "write_file")["function"]


def test_unoffered_forbidden_tool_is_still_refused(synthetic):
    effects, _ = synthetic
    client, _, _ = run([[call("write_file", path="/tmp/x.txt", content="hi")], "Synthetic answer."],
                       ask_quality=True, model_led_stub_rank=[], forbidden_tools=frozenset({"write_file"}))
    assert effects == []
    assert receipts(client.requests[1]) == []
    assert "forbidden" in tool_results(client.requests[1])[0]


def test_core_stubs_include_the_immediate_sends_and_forget():
    from service.model_only import ASK_CORE_STUBS
    assert {"send_email", "send_message", "schedule_send", "forget", "create_note",
            "find_free_time", "wisp_status"} <= ASK_CORE_STUBS


# --- compose / writing envelopes ----------------------------------------------

@pytest.mark.parametrize("prompt", [
    "Draft an email to mom saying I'll pick her up at the airport",                       # mail_draft_mom
    "draft an email to the landlord asking whether the laundry room will be open",        # mail_draft_landlord
    "write a message to my mom with my upcoming events for the next month",                # compose_draft_events_message
])
def test_a_message_addressed_to_someone_is_not_compose_only(prompt):
    from service.model_only import compose_only
    assert not compose_only(prompt)


@pytest.mark.parametrize("prompt", [
    "Write a short thank-you note to my professor for the extension.",
    "Compose a birthday message for my sister", "Write a haiku about autumn"])
def test_text_with_no_destination_stays_compose_only(prompt):
    from service.model_only import compose_only
    assert compose_only(prompt)


def test_writing_from_my_upcoming_events_keeps_its_reads():
    from service.model_only import pure_writing
    assert not pure_writing("write a message to my mom with my upcoming events for the next month")


# --- "when is ..." envelopes --------------------------------------------------

@pytest.mark.parametrize("prompt", ["When is mom's birthday?", "When is Sam's birthday?"])
def test_when_is_a_non_event_subject_keeps_its_ordinary_sources(prompt):
    # contact_mom_birthday: every tool used to be forbidden. (mail_water_shutoff
    # stays web-only: the web classifier reads it as a public question.)
    from service.router.router import routing_guard_contract
    import service.tools  # noqa: F401
    forbidden, _ = routing_guard_contract(prompt)
    assert not {"lookup_contact", "contact_dates", "view_emails", "summarize_emails"} & forbidden


@pytest.mark.parametrize("prompt", ["When is the internal budget review?", "When is Jordan's board review?"])
def test_when_is_an_unowned_event_still_grants_nothing(prompt):
    from service.router.router import routing_guard_contract
    import service.tools  # noqa: F401
    forbidden, _ = routing_guard_contract(prompt)
    assert set(registry.REGISTRY) <= forbidden


# --- a bare assent to the assistant's own send offer ---------------------------

@pytest.mark.parametrize("prompt,last", [
    ("sure", "Friday at 6:40 PM at SFO. I can text her that if you'd like?"),        # fu_sure_after_offer
    ("messages", "Should I send that to your mom through Messages or email?"),       # fu_channel_messages_bare
    ("yes please", "Want me to email Dad the schedule?"),
])
def test_assent_to_a_send_offer_reaches_only_always_confirmed_sends(prompt, last):
    from service.router.model_led import continuation_send_exemption
    from service.safety.policy import ALWAYS_CONFIRM_CATEGORIES
    exempt = continuation_send_exemption(prompt, last)
    assert exempt == {"messages_send", "email_send"}
    assert exempt <= ALWAYS_CONFIRM_CATEGORIES


@pytest.mark.parametrize("prompt,last", [
    ("no", "I can text her that if you'd like?"),
    ("never mind", "Should I send that through Messages or email?"),
    ("sure", "Want me to delete the old reminders?"),
    ("yes", "Your calendar today: dentist at 3."),
    ("text her that the flight is late", "I can text her that if you'd like?"),   # not a bare assent
])
def test_no_send_exemption_without_an_assent_to_a_send_offer(prompt, last):
    from service.router.model_led import continuation_send_exemption
    assert continuation_send_exemption(prompt, last) == frozenset()


# --- skill / MCP actions get the host's completion receipt ---------------------

def test_skill_runner_returns_a_host_receipt_only_on_exit_zero(monkeypatch, tmp_path):
    # dev_mac_lock_screen, dev_mac_pause_spotify, dev_mac_volume_down.
    from service.router.model_led import BuiltinCompletion, has_effect_contract, trusted_effect_receipt
    from service.skills import tools as skill_tools
    from service.tools import classify_tool_outcome

    class Proc:
        def __init__(self, code):
            self.returncode = code

        async def communicate(self):
            return b"screen locked", None

    codes = iter([0, 3])

    async def fake_exec(*argv, **kw):
        return Proc(next(codes))
    monkeypatch.setattr(skill_tools.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(skill_tools.sandbox, "available", lambda: False)
    runner = skill_tools._make_runner(tmp_path, {"command": "lockit"}, "mac_lock_x")
    tool = registry.Tool("mac_lock_x", "d", {"type": "object", "properties": {}}, "skill_tool", runner)
    monkeypatch.setattr(skill_tools, "_registered", {"mac_lock_x"})
    assert skill_tools.authentic_completion_tool(tool)
    assert has_effect_contract(tool, classify_tool_outcome(tool.name, ""))
    ok = asyncio.run(runner())
    assert type(ok) is BuiltinCompletion and ok == "screen locked" and ok.completion_code == 0
    assert trusted_effect_receipt(tool, classify_tool_outcome(tool.name, str(ok)), ok)
    failed = asyncio.run(runner())
    assert type(failed) is str and failed.startswith("(exit code 3)")
    assert not trusted_effect_receipt(tool, classify_tool_outcome(tool.name, failed), failed)


def test_a_forged_skill_function_is_never_a_receipt(monkeypatch):
    from service.router.model_led import BuiltinCompletion, has_effect_contract, trusted_effect_receipt
    from service.skills import tools as skill_tools
    from service.tools import classify_tool_outcome

    async def forged(**_):
        return BuiltinCompletion("done", tool_name="mac_forged", completion_code=0)
    forged._wisp_completion = object()          # not the module's token
    forged._wisp_tool_name = "mac_forged"
    tool = registry.Tool("mac_forged", "d", {"type": "object", "properties": {}}, "skill_tool", forged)
    monkeypatch.setattr(skill_tools, "_registered", {"mac_forged"})
    assert not skill_tools.authentic_completion_tool(tool)
    assert not has_effect_contract(tool, classify_tool_outcome(tool.name, ""))
    result = asyncio.run(forged())
    assert not trusted_effect_receipt(tool, classify_tool_outcome(tool.name, str(result)), result)


def test_mcp_caller_receipt_follows_the_protocol_error_flag(monkeypatch):
    # safe_mcp_drive_create.
    import service.mcp as mcp
    from service.router.model_led import BuiltinCompletion, has_effect_contract
    from service.tools import classify_tool_outcome

    class Server:
        name, running, error = "drive", True, ""

        def __init__(self):
            self.replies = iter(["Created document", mcp._render_content(
                {"isError": True, "content": [{"type": "text", "text": "quota"}]})])

        async def call_tool(self, remote, args):
            return next(self.replies)
    caller = mcp._make_caller(Server(), "create_document", {}, "mcp_drive_create_x")
    tool = registry.Tool("mcp_drive_create_x", "d", {"type": "object", "properties": {}}, "mcp_action", caller)
    monkeypatch.setattr(mcp.manager, "_registered", {"mcp_drive_create_x"})
    assert mcp.authentic_completion_tool(tool)
    assert has_effect_contract(tool, classify_tool_outcome(tool.name, ""))
    ok, failed = asyncio.run(caller()), asyncio.run(caller())
    assert type(ok) is BuiltinCompletion and ok.completion_code == 0
    assert type(failed) is BuiltinCompletion and failed.completion_code == 1


# --- by-title writes ----------------------------------------------------------

def _with_store(store, coro_fn):
    from service.tools import assistant_tools
    real = assistant_tools.assistant_store
    assistant_tools.assistant_store = store
    try:
        return asyncio.run(coro_fn())
    finally:
        assistant_tools.assistant_store = real


def _store():
    from service.assistant.store import AssistantStore
    return AssistantStore(Path(os.environ["WISP_HOME"]) / f"assistant-{time.time_ns()}.db")


def test_update_reminder_renames_without_a_new_time():
    # rem_rename_plants: "rename water the plants to water the garden".
    from service.tools import assistant_tools
    store = _store()
    when = (datetime.now() + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    store.add_manual("Water the plants", when.timestamp())
    result = _with_store(store, lambda: assistant_tools.update_reminder(
        title="water the plants", new_title="Water the garden"))
    assert result.startswith("Reminder updated: “Water the garden”"), result
    [row] = [r for r in store.upcoming(days=7) if r["title"] == "Water the garden"]
    assert abs(row["when_ts"] - when.timestamp()) < 1


def test_update_reminder_with_nothing_to_change_is_refused():
    from service.tools import assistant_tools
    result = _with_store(_store(), lambda: assistant_tools.update_reminder(title="water"))
    assert result.startswith("(error:") and "new_title" in result


def test_cancel_with_a_loose_title_names_the_close_titles_and_changes_nothing():
    # fu_cancel_class: cancel_event(title="yoga one").
    from service.tools import assistant_tools
    store = _store()
    store.add_manual("Yoga class", (datetime.now() + timedelta(days=2)).timestamp())
    result = _with_store(store, lambda: assistant_tools.cancel_event(title="yoga one"))
    assert "Nothing upcoming or past matches" in result and "“Yoga class”" in result
    assert [r["title"] for r in store.upcoming(days=7)] == ["Yoga class"]


def test_similar_titles_share_a_word_stem():
    from service.tools.title_match import similar_titles
    titles = ["Dentist appointment", "Yoga class", "Water the plants"]
    assert similar_titles("dentist thing", titles) == ["Dentist appointment"]
    assert similar_titles("watered the plants", titles) == ["Water the plants"]
    assert similar_titles("the one thing", titles) == []
