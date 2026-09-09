"""Offline end-to-end readiness and session-pin regressions; no live inference."""
from __future__ import annotations

import asyncio
import json

import pytest

from service import main
from service.config import role_to_model
from service.inference.readiness import TurnInferenceClient
from service.memory import context
from service.memory.store import SessionStore
from service.router import route
from service.router.pinning import apply_session_pin
from service.tools.registry import REGISTRY, Tool


@pytest.mark.parametrize("role", ["agent", "coding", "reasoning"])
@pytest.mark.parametrize("text", ["thanks", "hello", "Hello there!", "thank you", "good morning"])
def test_standalone_social_turn_keeps_fast_route(role, text):
    decision = asyncio.run(route(text))
    session = {"pinned_role": role, "pinned_model": role_to_model(role)}
    assert apply_session_pin(decision, session, text) is decision
    assert decision.role == "fast"
    assert not decision.needs_tools
    assert decision.tool_subset is None
    assert session["pinned_role"] == role


@pytest.mark.parametrize("text", ["okay shorter", "thanks shorter", "yes", "no", "never mind", "okay"])
def test_possible_task_continuations_keep_pin(text):
    decision = asyncio.run(route(text))
    decision = apply_session_pin(decision, {"pinned_role": "coding", "pinned_model": "code"}, text)
    assert decision.role == "coding"
    assert decision.model == "code"


def test_assent_context_remains_actionable_and_skill_remains_active():
    decision = asyncio.run(route("yes send it",
        last_user="send a text to Maya", last_assistant="I can send that text to Maya. Shall I send it?",
        last_tools="send_message"))
    decision = apply_session_pin(decision, {"pinned_role": "coding", "pinned_model": "code"}, "yes send it")
    assert decision.needs_tools
    assert "send_message" in decision.tool_subset
    social = asyncio.run(route("thanks"))
    apply_session_pin(social, {"pinned_role": "coding", "pinned_model": "code"},
                      "thanks", active_skill="interview-me")
    assert social.role == "coding"


class FakeClient:
    def __init__(self, calls):
        self.calls = calls
        self.messages = []

    async def ensure_only(self, model, **kwargs):
        self.calls.append(("load", model, kwargs))

    async def chat(self, model, messages, **kwargs):
        self.calls.append(("chat", model, kwargs))
        self.messages.append(messages)
        return {"choices": [{"message": {"content": "Preserved rolling summary."}}]}

    async def stream_events(self, model, messages, **kwargs):
        self.calls.append(("stream", model, kwargs))
        self.messages.append(messages)
        yield {"kind": "content", "text": "Grounded answer."}
        yield {"kind": "final", "message": {"role": "assistant", "content": "Grounded answer."}}


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    """Exercise the real endpoint/loop with isolated storage and tool fixtures."""
    calls = []
    raw = FakeClient(calls)
    store = SessionStore(tmp_path / "sessions.db")
    monkeypatch.setattr(main, "client", raw, raising=False)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(context, "store", store)
    monkeypatch.setattr(main, "models_config", lambda: {"tool_retrieval": {"provider": "lexical"}})

    async def start():
        calls.append(("start",))

    monkeypatch.setattr(main, "ensure_omlx", start)

    async def request(prompt, **kwargs):
        response = await main.agent({"prompt": prompt, "debug": False, **kwargs})
        events = []
        async for item in response.body_iterator:
            if isinstance(item, bytes):
                item = item.decode()
            events.append(json.loads(item.removeprefix("data: ").strip()))
        assert not [e for e in events if e["type"] == "error"], events
        return events

    return request, calls, raw, store


def fake_summary(monkeypatch, calls, result="Exact complete source digest."):
    original = REGISTRY["summarize_messages"]

    def summarize(**kwargs):
        calls.append(("tool", kwargs))
        return result

    monkeypatch.setitem(REGISTRY, "summarize_messages", Tool(
        name=original.name, description=original.description,
        parameters=original.parameters, category=original.category, func=summarize))


def test_direct_digest_returns_identical_text_without_start_or_load(endpoint, monkeypatch):
    request, calls, raw, _ = endpoint
    fake_summary(monkeypatch, calls)
    events = asyncio.run(request("summarize my messages"))
    assert calls == [("tool", {})]
    assert [e["text"] for e in events if e["type"] == "text"] == ["Exact complete source digest."]
    assert not raw.messages


def test_failed_direct_read_readies_before_model_fallback(endpoint, monkeypatch):
    request, calls, _, _ = endpoint
    fake_summary(monkeypatch, calls, "(error: source currently unavailable)")
    asyncio.run(request("summarize my messages"))
    assert [c[0] for c in calls[:4]] == ["tool", "start", "load", "stream"]
    assert calls[2][2]["exclusive"] is True


def test_plain_chat_loads_once_and_retains_memory_skills_before_clock(endpoint, monkeypatch):
    request, calls, raw, store = endpoint
    sid = store.create_session()
    store.set_pinned(sid, "agent", role_to_model("agent"))
    monkeypatch.setattr(main, "memory_block", lambda **kwargs: "\nREMEMBERED FACT")
    monkeypatch.setattr(main, "now_line", lambda: "\nEXACT CLOCK")
    monkeypatch.setattr(main.skills, "always_skills_block", lambda: "\nSTYLE SKILL")
    events = asyncio.run(request("thanks", session_id=sid))
    assert [c[0] for c in calls] == ["start", "load", "stream"]
    routed = next(e for e in events if e["type"] == "routed")
    assert routed["role"] == "fast" and routed["needs_tools"] is False
    system = raw.messages[0][0]["content"]
    assert system.index("REMEMBERED FACT") < system.index("STYLE SKILL") < system.index("EXACT CLOCK")
    assert store.get_session(sid)["pinned_role"] == "agent"


def test_test_mode_plain_chat_uses_no_inference(endpoint):
    request, calls, _, _ = endpoint
    events = asyncio.run(request("hello", test_mode=True))
    assert calls == []
    assert any("No tools needed" in e.get("text", "") for e in events)


def test_plain_chat_finishes_loading_before_chunk_timeout(endpoint, monkeypatch):
    request, calls, _, _ = endpoint
    real_wait_for = asyncio.wait_for

    async def wait_for_chunk(awaitable, timeout):
        assert [c[0] for c in calls[:2]] == ["start", "load"]
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(main.asyncio, "wait_for", wait_for_chunk)
    asyncio.run(request("hello"))


def test_pending_email_subject_thanks_reaches_approval_before_social_route(endpoint, monkeypatch):
    request, calls, _, store = endpoint
    from service.tasks.engine import prepare_task_turn
    sid = store.create_session()
    store.set_pinned(sid, "agent", role_to_model("agent"))
    initial = prepare_task_turn(store, sid, "email fixture@example.com saying See you soon",
                                assistant_store=main.assistant_store,
                                contacts_resolver=lambda _: [])
    assert initial is not None and not initial.executable
    approvals = []

    class DenyApprover:
        def __init__(self, emit):
            pass

        async def confirm(self, action):
            approvals.append(action)
            return False

    monkeypatch.setattr(main, "InteractiveApprover", DenyApprover)
    original = REGISTRY["send_email"]

    def no_send(**kwargs):
        pytest.fail("Denied message must never execute")

    monkeypatch.setitem(REGISTRY, "send_email", Tool(
        name=original.name, description=original.description,
        parameters=original.parameters, category=original.category, func=no_send))
    events = asyncio.run(request("thanks", session_id=sid))
    assert len(approvals) == 1
    assert approvals[0]["args"] == {
        "to": "fixture@example.com", "subject": "thanks", "body": "See you soon"}
    assert not any(e["type"] == "routed" for e in events)
    assert calls == []


def test_optional_reranker_is_ready_before_route(endpoint, monkeypatch):
    request, calls, _, _ = endpoint
    monkeypatch.setattr(main, "models_config", lambda: {"tool_retrieval": {"provider": "reranker"}})
    original_route = main.route

    async def routed(*args, **kwargs):
        assert calls == [("start",)]
        calls.append(("route",))
        return await original_route(*args, **kwargs)

    monkeypatch.setattr(main, "route", routed)
    asyncio.run(request("hello", test_mode=True))
    assert calls == [("start",), ("route",)]


def test_direct_turn_still_folds_overflow_memory_when_due(endpoint, monkeypatch):
    request, calls, _, store = endpoint
    fake_summary(monkeypatch, calls)
    sid = store.create_session()
    for i in range(context.KEEP_MESSAGES + context._FOLD_BATCH - 2):
        store.add_turn(sid, "user" if i % 2 == 0 else "assistant", f"Fixture turn {i}")
    asyncio.run(request("summarize my messages", session_id=sid))
    assert [c[0] for c in calls] == ["tool", "start", "load", "chat"]
    assert store.get_session(sid)["summary"] == "Preserved rolling summary."
    assert store.get_session(sid)["summarized_idx"] == context._FOLD_BATCH


def test_residency_rechecked_each_step_and_start_failures_propagate():
    async def run():
        calls = []

        async def start():
            calls.append(("start",))

        wrapped = TurnInferenceClient(FakeClient(calls), start)
        for _ in range(2):
            await wrapped.ensure_only("model", exclusive=True)
            async for _ in wrapped.stream_events("model", []):
                pass
        assert [c[0] for c in calls] == ["start", "load", "stream", "load", "stream"]

        async def broken_start():
            raise RuntimeError("engine unavailable")

        broken = TurnInferenceClient(FakeClient(calls), broken_start)
        with pytest.raises(RuntimeError, match="engine unavailable"):
            await broken.chat("model", [])

    asyncio.run(run())


def test_closing_wrapped_stream_closes_underlying_stream_immediately():
    async def run():
        calls = []

        class OpenStreamClient(FakeClient):
            async def stream_events(self, model, messages, **kwargs):
                try:
                    yield {"kind": "content", "text": "First"}
                    yield {"kind": "content", "text": "Second"}
                finally:
                    calls.append(("closed",))

        async def start():
            calls.append(("start",))

        wrapped = TurnInferenceClient(OpenStreamClient(calls), start)
        events = wrapped.stream_events("model", [])
        assert (await anext(events))["text"] == "First"
        assert ("closed",) not in calls
        await events.aclose()
        assert calls[-1] == ("closed",)

    asyncio.run(run())
