"""Synthetic regressions for read requests misclassified as deliveries."""
from __future__ import annotations

import asyncio
import json

import pytest

from service import main
from service.memory import context
from service.memory.store import SessionStore
from service.workflows.engine import prepare_turn
from service.workflows.models import WorkflowPlan


OUTBOUND_TOOLS = {
    "send_email", "send_message", "draft_email", "draft_message",
    "forward_email", "schedule_send", "lookup_contact",
}


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.db")
    offered: list[tuple[str, set[str]]] = []
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(context, "store", store)
    monkeypatch.setattr(main, "client", object(), raising=False)
    monkeypatch.setattr(
        main, "models_config", lambda: {"tool_retrieval": {"provider": "lexical"}})

    async def ready():
        return None

    async def synthetic_agent(_client, _model, messages, emit, _approver,
                              *, tools=None, **_kwargs):
        prompt = str(messages[-1]["content"])
        available = set(tools or ())
        offered.append((prompt, available))
        assert not available & OUTBOUND_TOOLS
        tool = "summarize_emails" if "email" in prompt.lower() else "summarize_messages"
        assert tool in available
        result = f"Synthetic {tool} result."
        await emit({"type": "tool_call", "id": "fixture-read", "name": tool, "args": {}})
        await emit({"type": "tool_result", "id": "fixture-read", "result": result,
                    "status": "succeeded"})
        await emit({"type": "text", "text": result})
        await emit({"type": "done"})
        return result

    monkeypatch.setattr(main, "ensure_omlx", ready)
    monkeypatch.setattr(main, "run_agent", synthetic_agent)

    def request(prompt: str, session_id: str | None = None):
        async def run():
            response = await main.agent({
                "prompt": prompt,
                "session_id": session_id,
                "debug": False,
            })
            found = []
            async for item in response.body_iterator:
                if isinstance(item, bytes):
                    item = item.decode()
                found.append(json.loads(item.removeprefix("data: ").strip()))
            return found

        events = asyncio.run(run())
        assert not [event for event in events if event["type"] == "error"], events
        sid = next(event["id"] for event in events if event["type"] == "session")
        return sid, events

    yield store, offered, request
    store._db.close()


@pytest.mark.parametrize("prompt", [
    "what is on my email",
    "tell me about my email",
    "show my inbox",
    "summarize my messages",
])
def test_personal_reads_bypass_legacy_delivery_compilation(prompt):
    routed_prompt, bypass = main._legacy_read_boundary(prompt, None)
    assert routed_prompt == prompt and bypass
    assert main._is_personal_read_request(prompt)


@pytest.mark.parametrize("prompt", [
    "what is on my email and can you send it to Mom",
    "email Mom what is in my inbox",
    "archive old emails",
])
def test_outbound_and_mutating_requests_are_not_weakened(prompt):
    assert not main._is_personal_read_request(prompt)
    assert main._legacy_read_boundary(prompt, None) == (prompt, False)


def test_genuine_delivery_channel_answer_still_advances_legacy_workflow():
    active = WorkflowPlan(
        original_request="send Mom my calendar summary",
        sources=["calendar"],
        status="waiting_for_channel",
    ).to_dict()
    assert main._legacy_read_boundary("Messages", active) == ("Messages", False)


def test_fresh_reported_read_routes_only_to_read_tools(endpoint):
    store, offered, request = endpoint
    sid, events = request("what is on my email")

    assert any(event.get("name") == "summarize_emails" for event in events)
    assert not any(event.get("name") in OUTBOUND_TOOLS for event in events)
    assert store.active_workflow(sid) is None
    assert store.last_assistant_turn(sid) == "Synthetic summarize_emails result."
    assert len(offered) == 1 and offered[0][0] == "what is on my email"


def test_reported_continuations_recover_old_session_without_send_slots(endpoint):
    store, offered, request = endpoint
    sid = store.create_session()

    # Synthetic reproduction of the persisted state produced by the old build.
    first = prepare_turn(store, sid, "what is on my email")
    assert first is not None and first.plan.status == "waiting_for_channel"
    store.add_turn(sid, "user", "what is on my email")
    store.add_turn(sid, "assistant", first.response)

    _, message_events = request("messages", sid)
    assert any(event.get("name") == "summarize_messages" for event in message_events)
    assert not any(event.get("name") in OUTBOUND_TOOLS for event in message_events)
    retired = store.workflow_state(sid, first.plan.id)
    assert retired["status"] == "cancelled"
    assert store.workflow_events(first.plan.id)[-1]["event"] == "read_intent_recovered"

    _, wisp_events = request("to me on wisp", sid)
    texts = [event["text"] for event in wisp_events if event["type"] == "text"]
    assert texts == ["The summary above is already displayed here in Wisp; nothing was sent elsewhere."]
    assert not any(event.get("name") in OUTBOUND_TOOLS for event in wisp_events)
    assert [prompt for prompt, _tools in offered] == ["show me my messages"]
    assert "recipient" not in " ".join(texts).lower()
    assert "content scope" not in " ".join(texts).lower()


def test_full_exported_failure_chain_retains_read_provenance(endpoint):
    store, offered, request = endpoint
    sid = store.create_session()
    turns = [
        ("what is on my email", "waiting_for_channel"),
        ("messages", "waiting_for_recipient"),
        ("to me on wisp", "waiting_for_content"),
    ]
    final = None
    for prompt, status in turns:
        final = prepare_turn(store, sid, prompt)
        assert final is not None and final.plan.status == status
        store.add_turn(sid, "user", prompt)
        store.add_turn(sid, "assistant", final.response)

    # An upgraded build recovers the user-authored inbox read even though the
    # final malformed workflow retained only the display-surface continuation.
    _, events = request("show it on wisp", sid)
    assert any(event.get("name") == "summarize_emails" for event in events)
    assert not any(event.get("name") in OUTBOUND_TOOLS for event in events)
    assert offered[0][0] == "what is on my email"
    assert store.workflow_state(sid, final.plan.id)["status"] == "cancelled"
