"""Actual /agent entrypoint with synthetic stores, inference and tools only.

The admitted controller must set disposable WISP_HOME before importing main.
No app lifespan, live provider, native tool, credential or background job runs.
"""
import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from service import main
from service.agent import loop
from service.config.endpoints import Endpoint, Target
from service.memory import context
from service.memory.store import SessionStore
from service.router.router import RouteDecision
from service.tools import registry


class FakeClient:
    def __init__(self, target, script=()):
        self.target = target
        self.managed = target.endpoint.managed
        self.endpoint_name = target.endpoint.name
        self.base_url = target.endpoint.base_url
        self.script = list(script)
        self.requests = []
        self.loads = []
        self.closed = False

    async def ensure_only(self, model, **kwargs):
        self.loads.append((model, kwargs))

    async def aclose(self):
        self.closed = True

    async def stream_events(self, model, messages, **kwargs):
        index = len(self.requests)
        self.requests.append(copy.deepcopy({"model": model, "messages": messages, **kwargs}))
        result = self.script[index] if index < len(self.script) else "Synthetic answer."
        message = {"role": "assistant", "content": "", "tool_calls": None}
        if isinstance(result, list):
            message["tool_calls"] = result
        else:
            message["content"] = result
        yield {"kind": "final", "message": message}


def tool_call(tool_name, **args):
    return {"id": "fixture-" + tool_name, "function": {
        "name": tool_name, "arguments": json.dumps(args)}}


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    target = Target("agent", Endpoint("local", "http://127.0.0.1:8000", "local_omlx", True),
                    "Ling-3.0-tiny-Wisp-V2-merged-oQ4e", context_window=32000)
    store = SessionStore(tmp_path / "sessions.db")
    state = SimpleNamespace(target=target, shared=FakeClient(target), owned=[],
                            agent_calls=[], baseline_calls=[], effects=[], script=[],
                            starts=0, summaries=0, active_skill="")
    monkeypatch.setenv("WISP_MODEL_LED_ROUTING", "1")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(context, "store", store)
    monkeypatch.setattr(main, "client", state.shared, raising=False)
    monkeypatch.setattr(main, "role_target", lambda role: state.target)
    monkeypatch.setattr(main, "cloud_super_model_enabled", lambda: False)
    monkeypatch.setattr(main, "models_config", lambda: {"tool_retrieval": {"provider": "lexical"}})
    monkeypatch.setattr("service.memory.prompt_blocks.memory_block", lambda **kwargs: "")
    monkeypatch.setattr(main.skills, "select_for_turn", lambda *args: state.active_skill)
    monkeypatch.setattr(main.skills, "turn_skill_names", lambda *args: [])
    monkeypatch.setattr(main.skills, "digest_used_skill", lambda *args: False)
    monkeypatch.setattr(main, "prepare_turn", lambda *args, **kwargs: None)
    monkeypatch.setattr("service.workflows.engine.prepare_news_selector_guard", lambda *args: None)

    async def no_task(*args, **kwargs):
        return None
    monkeypatch.setattr("service.tasks.reply_engine.prepare_task_turn_async", no_task)
    from service.workflows import reads
    state.original_compile_read = reads.compile_read
    monkeypatch.setattr(reads, "compile_read", lambda *args, **kwargs: None)

    async def baseline(prompt, **kwargs):
        state.baseline_calls.append((prompt, kwargs))
        return RouteDecision(role="general", model=target.model, needs_tools=False,
                             source="default", reason="Synthetic baseline")
    monkeypatch.setattr(main, "route", baseline)

    async def start():
        state.starts += 1
    async def summarize(*args, **kwargs):
        state.summaries += 1
    monkeypatch.setattr(main, "ensure_omlx", start)
    monkeypatch.setattr(main, "maybe_summarize", summarize)
    monkeypatch.setattr(main.idle, "begin_foreground", lambda: None)
    monkeypatch.setattr(main.idle, "end_foreground", lambda: None)

    def own(*, target):
        client = FakeClient(target, state.script)
        state.owned.append(client)
        return client
    monkeypatch.setattr(main, "OMLXClient", own)

    async def capture(client, model, messages, emit, approver, **kwargs):
        state.agent_calls.append(copy.deepcopy({"model": model, "messages": messages, **kwargs}))
        await emit({"type": "text", "text": "Synthetic answer."})
        await emit({"type": "done"})
        return "Synthetic answer."
    monkeypatch.setattr(main, "run_agent", capture)

    # Replace the whole inventory, including native and extension tools.
    fake_registry = {}
    monkeypatch.setattr(registry, "REGISTRY", fake_registry)
    async def upcoming(**args):
        state.effects.append(("get_upcoming", copy.deepcopy(args)))
        return "Synthetic Calendar: Workshop tomorrow at 14:00."
    upcoming.__module__ = "service.tools.assistant_tools"
    fake_registry["get_upcoming"] = registry.Tool("get_upcoming", "Synthetic calendar read",
        {"type": "object", "properties": {"days": {"type": "integer"},
         "period": {"type": "string"},
         "calendar_only": {"type": "boolean"}}, "additionalProperties": False},
        "assistant_read", upcoming)

    async def request(prompt, sid=None, *, allow_errors=False, **kwargs):
        response = await main.agent({"prompt": prompt, "session_id": sid, "debug": False, **kwargs})
        events = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode()
            events.append(json.loads(chunk.removeprefix("data: ").strip()))
        errors = [event for event in events if event["type"] == "error"]
        if not allow_errors:
            assert not errors, errors
        return events
    state.request = lambda prompt, sid=None, **kwargs: asyncio.run(request(prompt, sid, **kwargs))
    state.store = store
    try:
        yield state
    finally:
        store._db.close()
        with pytest.raises(sqlite3.ProgrammingError):
            store._db.execute("SELECT 1")
        assert not main.SESSIONS, "Request registry leaked"
        assert all(client.closed for client in state.owned), "Owned transport leaked"
        for path in tmp_path.glob("sessions.db*"):
            path.unlink(missing_ok=True)


def test_explicit_rollback_keeps_baseline_and_never_allocates_owned_client(endpoint, monkeypatch):
    monkeypatch.setenv("WISP_MODEL_LED_ROUTING", "0")
    endpoint.request("hello", test_mode=True)
    assert len(endpoint.baseline_calls) == 1
    assert not endpoint.agent_calls and not endpoint.owned
    assert endpoint.starts == 0


@pytest.mark.parametrize("prompt", ["what is up this weej", "what did Mom say"])
def test_original_prompt_reaches_model_without_positive_rule_selection(endpoint, prompt):
    events = endpoint.request(prompt)
    call = endpoint.agent_calls[0]
    assert call["messages"][-1] == {"role": "user", "content": prompt}
    assert call["model_led_discovery"] is True and call["tools"] == []
    assert call["force_first_tool"] is None and not call["direct_calls"]
    assert not endpoint.baseline_calls
    assert next(e for e in events if e["type"] == "routed")["route_source"] == "model_led_discovery"
    assert len(endpoint.owned) == 1 and endpoint.owned[0].closed
    sid = next(e for e in events if e["type"] == "session")["id"]
    assert endpoint.store.last_user_turn(sid) == prompt
    assert endpoint.store.last_assistant_turn(sid) == "Synthetic answer."


def test_tomorrow_followup_retains_conversation_even_with_coding_pin(endpoint):
    sid = endpoint.store.create_session()
    endpoint.store.set_pinned(sid, "coding", "another-model")
    endpoint.store.add_turn(sid, "user", "What is on my calendar this week?")
    endpoint.store.add_turn(sid, "assistant", "Calendar: Workshop on Thursday.", tool_digest="get_upcoming")
    endpoint.request("and tomorrow", sid)
    call = endpoint.agent_calls[0]
    contents = [m.get("content") for m in call["messages"]]
    assert "What is on my calendar this week?" in contents
    assert "Calendar: Workshop on Thursday." in contents
    assert contents[-1] == "and tomorrow"
    assert call["model"] == endpoint.target.model and call["model_led_discovery"]


def test_current_calendar_keeps_references_but_labels_old_answer_unverified(endpoint):
    sid = endpoint.store.create_session()
    endpoint.store.add_turn(sid, "user", "What is on my calendar?")
    endpoint.store.add_turn(sid, "assistant", "Old stale event.", tool_digest="get_upcoming")
    endpoint.request("what is on my calendar this week", sid)
    call = endpoint.agent_calls[0]
    assert any("Old stale event" in str(m.get("content", "")) for m in call["messages"])
    assert "unverified context" in call["messages"][0]["content"]
    assert call["include_memory_context"] is True


def test_read_exclusions_and_calendar_binding_reach_existing_executor(endpoint):
    endpoint.request("check my calendar this week but don't read reminders and don't read email")
    call = endpoint.agent_calls[0]
    assert {"view_emails", "search_reminders"} <= set(call["forbidden_tools"])
    assert call["tool_argument_bindings"]["get_upcoming"]["calendar_only"] is True


@pytest.mark.parametrize("kind", ["unmanaged", "different_origin"])
def test_unadmitted_inference_target_keeps_baseline(endpoint, kind):
    if kind == "unmanaged":
        endpoint.shared.managed = False
    else:
        endpoint.shared.base_url = "http://127.0.0.1:9999"
    events = endpoint.request("hello", test_mode=True, allow_errors=True)
    assert any(e["type"] == "error" for e in events)
    assert not endpoint.baseline_calls
    assert not endpoint.agent_calls and not endpoint.owned


@pytest.mark.parametrize("prompt,question", [
    ("yes", "Shall I send it?"), ("Messages", "Messages or email?"),
    ("never mind", "What would you like to change?")])
def test_pending_confirmation_and_short_clarification_reach_model(endpoint, prompt, question):
    sid = endpoint.store.create_session()
    endpoint.store.add_turn(sid, "user", "Help with this request")
    endpoint.store.add_turn(sid, "assistant", question)
    endpoint.request(prompt, sid)
    assert not endpoint.baseline_calls
    assert endpoint.agent_calls[0]["messages"][-1]["content"] == prompt


def test_active_skill_retains_model_ownership(endpoint):
    endpoint.active_skill = "fixture-skill"
    endpoint.request("help me plan", test_mode=True)
    assert not endpoint.baseline_calls
    assert endpoint.agent_calls[0]["model_led_discovery"]


def test_new_typed_compiler_never_runs_before_discovery(endpoint, monkeypatch):
    async def task(*args, **kwargs):
        raise AssertionError("New positive compiler bypassed model ownership")
    monkeypatch.setattr("service.tasks.reply_engine.prepare_task_turn_async", task)
    events = endpoint.request("which recipient", test_mode=True)
    assert endpoint.agent_calls and not endpoint.baseline_calls


def test_actual_entrypoint_discovers_then_executes_only_synthetic_calendar(endpoint, monkeypatch):
    monkeypatch.setattr(main, "run_agent", loop.run_agent)
    monkeypatch.setattr(loop, "_BLOCKS_CACHE", None)
    monkeypatch.setattr(loop, "narration_mode", lambda: "off")
    monkeypatch.setattr(loop, "no_thinking_kwargs", lambda *args, **kwargs: {})
    monkeypatch.setattr("service.memory.identity.identity_prompt_block", lambda **kwargs: "")
    monkeypatch.setattr("service.skills.skills_context_block", lambda *args: "")
    monkeypatch.setattr("service.safety.policy._FULL_ACCESS", False)
    monkeypatch.setattr("service.safety.policy._READ_ONLY", False)
    monkeypatch.setattr("service.safety.grants.check", lambda *args: None)
    endpoint.script = [[tool_call("get_tool_schemas", families=["calendar"], tools=["get_upcoming"])],
                       [tool_call("get_upcoming", days=1, calendar_only=True)],
                       "Calendar: Workshop tomorrow at 14:00."]
    events = endpoint.request("what is up tommrow")
    client = endpoint.owned[0]
    names = lambda request: [t["function"]["name"] for t in request["tools"]]
    assert names(client.requests[0]) == ["get_tool_schemas"]
    assert "get_upcoming" in names(client.requests[1])
    assert endpoint.effects == [("get_upcoming", {"days": 1, "calendar_only": True})]
    assert client.closed and not endpoint.shared.requests
    assert any(e["type"] == "tool_result" for e in events)
    assert any(e.get("text") == "Calendar: Workshop tomorrow at 14:00." for e in events)
    assert not main.SESSIONS


def test_admitted_test_mode_discards_session_history_and_does_not_persist(endpoint):
    sid = endpoint.store.create_session()
    endpoint.store.add_turn(sid, "user", "Earlier private question")
    endpoint.store.add_turn(sid, "assistant", "Earlier private answer")
    before = endpoint.store.turns_range(sid, 0, 100)
    sessions = endpoint.store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    events = endpoint.request("what is up this weej", sid, test_mode=True)
    assert endpoint.agent_calls[0]["messages"][-1] == {"role": "user", "content": "what is up this weej"}
    assert not any("Earlier private" in str(m) for m in endpoint.agent_calls[0]["messages"])
    assert endpoint.agent_calls[0]["test_mode"] is True
    assert endpoint.store.turns_range(sid, 0, 100) == before
    assert endpoint.store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == sessions
    assert next(e for e in events if e["type"] == "session")["id"] != sid
    assert endpoint.summaries == 0


def test_turn_target_stays_captured_when_role_binding_changes(endpoint, monkeypatch):
    original = endpoint.target
    changed = Target("agent", original.endpoint, "a-different-model", context_window=8000)
    def own(*, target):
        endpoint.target = changed
        client = FakeClient(target)
        endpoint.owned.append(client)
        return client
    monkeypatch.setattr(main, "OMLXClient", own)
    endpoint.request("what did Mom say")
    assert endpoint.owned[0].target is original
    assert endpoint.agent_calls[0]["model"] == original.model
    assert endpoint.owned[0].closed


def test_failed_turn_closes_owned_client_and_persists_failure(endpoint, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("Synthetic inference failure")
    monkeypatch.setattr(main, "run_agent", fail)
    events = endpoint.request("what is up this weej", allow_errors=True)
    assert any(e["type"] == "error" for e in events)
    assert endpoint.owned[0].closed and not main.SESSIONS
    sid = next(e for e in events if e["type"] == "session")["id"]
    assert endpoint.store.last_user_turn(sid) == "what is up this weej"
    assert "could not be completed" in endpoint.store.last_assistant_turn(sid)


def test_disconnect_cancels_active_turn_and_closes_owned_client(endpoint, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(main, "run_agent", pending)
        response = await main.agent({"prompt": "what is up this weej", "debug": False})
        iterator = response.body_iterator
        first = await anext(iterator)
        sid = json.loads(first.removeprefix("data: ").strip())["id"]
        await asyncio.wait_for(entered.wait(), 1)
        await iterator.aclose()
        assert endpoint.owned[0].closed and not main.SESSIONS
        assert endpoint.store.last_user_turn(sid) == "what is up this weej"
        assert "disconnected" in endpoint.store.last_assistant_turn(sid)
        assert not endpoint.effects
    asyncio.run(scenario())


def test_default_off_preserves_direct_calendar_read(endpoint, monkeypatch):
    from service.workflows import reads
    # Fixture normally suppresses direct reads so semantic gating can be isolated.
    # Restore the real compiler/executor with the entirely synthetic registry.
    monkeypatch.setenv("WISP_MODEL_LED_ROUTING", "0")
    # The function imported by reads was monkeypatched on the module; recover its
    # exact original code via the fixture's saved callable, not a second import.
    monkeypatch.setattr(reads, "compile_read", endpoint.original_compile_read)
    events = endpoint.request("what is on my calendar this week")
    assert endpoint.effects == [("get_upcoming", {"period": "this week"})]
    assert not endpoint.owned and not endpoint.agent_calls and not endpoint.baseline_calls
    assert endpoint.starts == 0
    assert any(e["type"] == "done" for e in events)


def test_actual_loop_dry_run_keeps_discovery_but_never_executes_tool(endpoint, monkeypatch):
    monkeypatch.setattr(main, "run_agent", loop.run_agent)
    monkeypatch.setattr(loop, "_BLOCKS_CACHE", None)
    monkeypatch.setattr(loop, "narration_mode", lambda: "off")
    monkeypatch.setattr(loop, "no_thinking_kwargs", lambda *args, **kwargs: {})
    monkeypatch.setattr("service.memory.identity.identity_prompt_block", lambda **kwargs: "")
    monkeypatch.setattr("service.skills.skills_context_block", lambda *args: "")
    endpoint.script = [[tool_call("get_tool_schemas", families=["calendar"], tools=["get_upcoming"])],
                       [tool_call("get_upcoming", days=1, calendar_only=True)], "Synthetic dry-run plan."]
    events = endpoint.request("what is up tommrow", test_mode=True)
    assert not endpoint.effects and endpoint.owned[0].closed
    assert any(e["type"] == "tool_call" and e.get("name") == "get_upcoming" for e in events)
    assert endpoint.store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
