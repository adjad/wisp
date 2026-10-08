"""Actual discovery hooks/executor with scripted clients and synthetic tools.

These test plumbing and deterministic boundaries, never Ling's accuracy. The
closed controller must set disposable WISP_HOME before importing this module.
No native tool implementation, model, server lifespan or real user data runs.
"""
import asyncio
import copy
import json
import pytest

from service.agent import loop
from service.router.model_led import DISCOVERY_TOOL
from service.router.router import routing_guard_contract
from service.tools import registry


def call(name, **args):
    return {"id": f"synthetic-{name}", "function": {
        "name": name, "arguments": json.dumps(args)}}


def discover(family, *names):
    args = {"families": [family]}
    if names:
        args["tools"] = list(names)
    return call(DISCOVERY_TOOL, **args)


class ScriptedClient:
    managed = True
    endpoint_name = "omlx"
    context_window = 32000

    def __init__(self, script, before_step=None):
        self.script, self.requests = script, []
        self.before_step = before_step
        self.target = self  # Actual fitter reads target.context_window, not the client field.

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, **kwargs):
        step = len(self.requests)
        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        if self.before_step:
            self.before_step(step)
        response = self.script[step] if step < len(self.script) else "Synthetic final answer."
        message = {"role": "assistant", "content": "", "tool_calls": None}
        if isinstance(response, list):
            message["tool_calls"] = response
        else:
            message["content"] = response
        yield {"kind": "final", "message": message}


class Approval:
    def __init__(self, allowed=False):
        self.allowed, self.calls = allowed, []

    async def confirm(self, action):
        self.calls.append(copy.deepcopy(action))
        return self.allowed


@pytest.fixture
def synthetic(monkeypatch):
    effects = []
    fake_registry = {}
    monkeypatch.setattr(registry, "REGISTRY", fake_registry)
    monkeypatch.setattr(loop, "_BLOCKS_CACHE", None)
    monkeypatch.setattr(loop, "narration_mode", lambda: "off")
    monkeypatch.setattr(loop, "no_thinking_kwargs", lambda *args, **kwargs: {})
    monkeypatch.setattr("service.memory.identity.identity_prompt_block", lambda **kwargs: "")
    monkeypatch.setattr("service.skills.skills_context_block", lambda *args: "")
    # Confirmation policy remains real in scoped confirmation mode, with no grant.
    monkeypatch.setattr("service.safety.policy._FULL_ACCESS", False)
    monkeypatch.setattr("service.safety.policy._READ_ONLY", False)
    monkeypatch.setattr("service.safety.grants.check", lambda *args: None)

    def register(name, module, category="assistant_read", *, parameters=None, unavailable=""):
        async def fake(**args):
            effects.append((name, copy.deepcopy(args)))
            return f"Synthetic {name} result: Workshop tomorrow at 14:00."
        fake.__module__ = "service.tools." + module
        fake_registry[name] = registry.Tool(name, "Synthetic tool; no native effect",
            parameters or {"type": "object", "properties": {}, "additionalProperties": False},
            category, fake, unavailable_reason=unavailable)
        return fake_registry[name]

    register("get_upcoming", "assistant_tools", parameters={"type": "object", "properties": {
        "days": {"type": "integer"}, "period": {"type": "string"},
        "calendar_only": {"type": "boolean"}}, "additionalProperties": False})
    register("view_emails", "email_tools")
    register("view_messages", "imessage_tools")
    register("write_file", "files_tools", "fs_write", parameters={"type": "object", "properties": {
        "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]})
    register("run_shell", "builtin", "shell", parameters={"type": "object", "properties": {
        "cmd": {"type": "string"}}, "required": ["cmd"]})
    return effects, register


def run(script, *, prompt="what is up this weej?", history=(), approval=None, before_step=None, **kwargs):
    events = []
    client = ScriptedClient(script, before_step)
    async def emit(event):
        events.append(copy.deepcopy(event))
    async def execute():
        return await loop.run_agent(client, "synthetic-agent", [*history, {"role": "user", "content": prompt}],
            emit, approval or Approval(), tools=[], model_led_discovery=True,
            multi_round=True, include_memory_context=False, debug=True, **kwargs)
    answer = asyncio.run(execute())
    return client, events, answer


def names(request):
    return {schema["function"]["name"] for schema in request.get("tools") or []}


def test_baseline_default_does_not_offer_discovery(synthetic):
    client = ScriptedClient(["Synthetic baseline."])
    async def emit(event):
        pass
    asyncio.run(loop.run_agent(client, "synthetic-agent", [{"role": "user", "content": "hello"}],
        emit, Approval(), tools=["get_upcoming"], include_memory_context=False, debug=False))
    assert names(client.requests[0]) == {"get_upcoming"}


def test_schema_first_then_real_executor_arguments_and_receipts(synthetic):
    effects, _ = synthetic
    client, events, _ = run([[discover("calendar", "get_upcoming")],
                           [call("get_upcoming", period="this week", calendar_only=True)], "Synthetic agenda."])
    assert names(client.requests[0]) == {DISCOVERY_TOOL}
    assert names(client.requests[1]) == {DISCOVERY_TOOL, "get_upcoming"}
    schema = next(s for s in client.requests[1]["tools"] if s["function"]["name"] == "get_upcoming")
    assert schema == registry.get_tool("get_upcoming").schema()
    assert effects == [("get_upcoming", {"period": "this week", "calendar_only": True})]
    assert [e["name"] for e in events if e["type"] == "tool_call"] == ["get_upcoming"]
    assert not any(e.get("name") == DISCOVERY_TOOL and e["type"] == "tool_call" for e in events)
    receipt = next(m for m in client.requests[1]["messages"] if m.get("role") == "tool")
    assert json.loads(receipt["content"])["source_tools_executed"] is False


def test_same_response_schema_selection_does_not_admit_sibling_real_call(synthetic):
    effects, _ = synthetic
    client, events, _ = run([[discover("calendar", "get_upcoming"), call("get_upcoming", days=1)],
                           [call("get_upcoming", days=1)], "Synthetic agenda."])
    assert effects == [("get_upcoming", {"days": 1})]
    assert any(e["type"] == "tool_result" and "not available" in e["result"] for e in events)
    assert names(client.requests[0]) == {DISCOVERY_TOOL}


@pytest.mark.parametrize("prompt,history", [
    ("and tommrow?", [{"role": "user", "content": "my calendar this week"},
                      {"role": "assistant", "content": "Old synthetic agenda."}]),
    ("what did mom say?", []), ("what is up this weej?", []),
])
def test_original_prompt_and_followup_context_reach_model_intact(synthetic, prompt, history):
    client, _, _ = run(["Which source should I check?"], prompt=prompt, history=history)
    request = client.requests[0]
    assert request["messages"][-1] == {"role": "user", "content": prompt}
    assert all(message in request["messages"] for message in history)
    assert names(request) == {DISCOVERY_TOOL}
    assert synthetic[0] == []


def test_second_discovery_can_add_missing_family(synthetic):
    client, _, _ = run([[discover("calendar", "get_upcoming")],
                       [call("get_upcoming", days=1)], [discover("messages_contacts", "view_messages")],
                       [call("view_messages")], "Synthetic combined answer."])
    assert names(client.requests[3]) == {"get_upcoming", "view_messages"}
    assert [n for n, _ in synthetic[0]] == ["get_upcoming", "view_messages"]


def test_excluded_and_unavailable_tools_cannot_expand_or_execute(synthetic):
    effects, register = synthetic
    register("unavailable_read", "email_tools", unavailable="No synthetic implementation")
    client, events, _ = run([[discover("mail", "view_emails")],
                           [discover("calendar", "get_upcoming")],
                           [call("view_emails"), call("unavailable_read"), call("get_upcoming", days=1)],
                           "Synthetic answer."], forbidden_tools=frozenset({"view_emails"}))
    assert effects == [("get_upcoming", {"days": 1})]
    assert [e["status"] for e in events if e["type"] == "routing_discovery"] == ["rejected", "ready_next_step"]
    assert all("view_emails" not in names(r) and "unavailable_read" not in names(r) for r in client.requests)


def test_malformed_selection_consumes_budget_without_tool_effect(synthetic):
    client, events, _ = run([[call(DISCOVERY_TOOL, families=["unknown"])],
                           [call(DISCOVERY_TOOL, families="calendar")], "Please clarify the source."])
    assert DISCOVERY_TOOL not in names(client.requests[2])
    assert synthetic[0] == []
    assert len([e for e in events if e["type"] == "routing_discovery" and e["status"] == "rejected"]) == 2


def test_actual_argument_validator_rejects_bad_integer(synthetic):
    _, events, _ = run([[discover("calendar", "get_upcoming")],
                       [call("get_upcoming", days="tomorrow")], "Please use a valid date range."])
    assert synthetic[0] == []
    assert any(e["type"] == "tool_result" and "days" in e["result"] for e in events)


def test_calendar_only_binding_overrides_model(synthetic):
    run([[discover("calendar", "get_upcoming")], [call("get_upcoming", days=1, calendar_only=False)],
         "Synthetic calendar."], tool_argument_bindings={"get_upcoming": {"calendar_only": True}})
    assert synthetic[0] == [("get_upcoming", {"days": 1, "calendar_only": True})]


def test_refusal_reaches_actual_policy_and_no_fake_write(synthetic):
    approval = Approval(False)
    _, events, _ = run([[discover("files", "write_file")],
        [call("write_file", path="/private/tmp/synthetic-routing-only.txt", content="Synthetic")]], approval=approval)
    assert len(approval.calls) == 1
    assert synthetic[0] == []
    assert any(e.get("decision") == "confirm" for e in events)


def test_policy_denies_dangerous_shell_without_confirm_or_effect(synthetic):
    approval = Approval(True)
    run([[discover("automation", "run_shell")], [call("run_shell", cmd="rm -rf /")]], approval=approval)
    assert not approval.calls
    assert synthetic[0] == []


def test_test_mode_still_never_executes_or_confirms(synthetic):
    approval = Approval(True)
    run([[discover("files", "write_file")],
         [call("write_file", path="/private/tmp/synthetic-only", content="Synthetic")], "Synthetic plan."],
        test_mode=True, approval=approval)
    assert not approval.calls
    assert synthetic[0] == []


@pytest.mark.parametrize("options", [
    {"force_first_tool": "get_upcoming"},
    {"direct_calls": [("get_upcoming", {"days": 1})]},
    {"required_tool_groups": (frozenset({"get_upcoming"}),)},
])
def test_owned_execution_plan_cannot_enter_discovery(synthetic, options):
    with pytest.raises(ValueError, match="owned execution plan"):
        run([], **options)


def test_unmanaged_provider_cannot_enter_discovery(synthetic):
    client = ScriptedClient([])
    client.managed = False
    async def emit(event):
        pass
    with pytest.raises(ValueError, match="managed local inference"):
        asyncio.run(loop.run_agent(client, "synthetic-agent", [{"role": "user", "content": "Synthetic"}],
            emit, Approval(), tools=[], model_led_discovery=True, include_memory_context=False))


def test_exclusion_blocks_aggregate_and_opaque_escape_families(synthetic):
    _, register = synthetic
    for name, module, category in [
        ("daily_brief", "everyday", "assistant_read"), ("read_file", "builtin", "fs_read"),
        ("search_notes", "notes_tools", "assistant_read"), ("extension_read", "dynamic", "mcp_read"),
        ("skill_read", "dynamic", "skill_tool"), ("recall", "memory_tools", "memory_read")]:
        register(name, module, category)
    forbidden, _ = routing_guard_contract("show my messages; don't read email")
    assert {"view_emails", "daily_brief", "read_file", "extension_read", "skill_read", "recall", "run_shell"} <= forbidden
    assert "view_messages" not in forbidden


def test_reminder_exclusion_binds_calendar_only_and_blocks_aggregate(synthetic):
    forbidden, bindings = routing_guard_contract("show my calendar but don't include reminders")
    assert bindings["get_upcoming"] == {"calendar_only": True}
    assert "daily_brief" in forbidden
    assert "get_upcoming" not in forbidden


def test_quoted_prohibition_is_data_not_an_exclusion(synthetic):
    forbidden, bindings = routing_guard_contract('Create a note titled "do not read email"')
    assert "view_emails" not in forbidden
    assert not bindings


def test_read_only_and_draft_prohibitions(synthetic):
    assert "write_file" in routing_guard_contract("read only; show my calendar")[0]
    forbidden, _ = routing_guard_contract("draft an email to Mom, don't send")
    assert {"send_email", "send_message", "schedule_send"} <= forbidden
