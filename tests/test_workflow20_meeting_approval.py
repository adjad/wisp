"""Actual loop/InteractiveApprover over synthetic Calendar and fake local transport."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from tests.test_workflow20_meeting import calendar, host_request, proposal_args
from service.agent import loop
from service.agent.approver import InteractiveApprover
from service.tools import schedule_extras as S
from service.tools.registry import classify_tool_outcome


class Client:
    managed = True
    base_url = "http://127.0.0.1:8000"

    def __init__(self, calls, final="Private block result reviewed."):
        self.calls = calls
        self.final = final
        self.requests = []

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, **kwargs):
        self.requests.append(json.loads(json.dumps(messages)))
        if len(self.requests) <= len(self.calls):
            call = self.calls[len(self.requests) - 1]
            name, args = call(messages) if callable(call) else call
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_" + str(len(self.requests)), "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
        else:
            message = {"role": "assistant", "content": self.final}
        yield {"kind": "final", "message": message}


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    import socket
    import subprocess
    from service.memory import identity
    def forbidden(*args, **kwargs):
        raise AssertionError("No network or native processes in approval fixtures")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(identity, "identity_prompt_block", lambda **kwargs: "")


def run(client, *, approve=True, mutate=None, direct_calls=None, tools=None, timeout=.1):
    events, audits, order = [], [], []
    async def scenario():
        async def emit(event):
            events.append(json.loads(json.dumps(event)))
            if event["type"] == "confirm":
                order.append("approval_requested")
                if mutate:
                    mutate(event, approver)
                if approve is not None:
                    order.append("approved" if approve else "denied")
                    assert approver.resolve(event["id"], approve)
        approver = InteractiveApprover(emit, timeout=timeout, request_id="synthetic-request")
        result = await loop.run_agent(client, "synthetic-local", [{"role": "user", "content":
            "Create a private calendar block after the dentist tomorrow."}], emit, approver,
            tools=tools or ["plan_meeting", "commit_private_meeting"],
            include_memory_context=False, multi_round=True, max_steps=4,
            direct_calls=direct_calls)
        assert not approver._pending
        return result
    # Keep the actual audit writer active; events also get checked by callers.
    return asyncio.run(scenario()), events, order


def planned_client(*, final="Private block result reviewed.", capture=None, transform=None):
    def commit(messages):
        result = next(m["content"] for m in reversed(messages) if m["role"] == "tool")
        args = json.loads(result.split(": ", 1)[1])["commit_args"]
        if capture is not None:
            capture.append(args)
        if transform:
            transform(args)
        return "commit_private_meeting", args
    return Client([("plan_meeting", {"title": "Meeting with Morgan", "duration_min": 30,
        "after_event_title": "Dentist", "event_period": "tomorrow"}), commit], final=final)


def test_actual_plan_then_approve_then_commit_keeps_authority_local_and_transient(host_request, monkeypatch):
    native = AsyncMock(return_value={"ok": True, "status": "succeeded", "error": "", "source_id": "native.synthetic"})
    monkeypatch.setattr("service.assistant.outbox.request", native)
    audits = []
    monkeypatch.setattr(loop, "audit", lambda event, **fields: audits.append((event, fields)))
    seen_args = []
    client = planned_client(capture=seen_args)
    result, events, order = run(client)
    assert native.await_count == 1 and len(seen_args) == 1
    reference = seen_args[0]["capability_token"]
    assert reference.startswith("wisp_meeting_ref_")
    assert reference in json.dumps(client.requests)
    for token in S._meeting_capabilities:
        assert token not in json.dumps([client.requests, events, audits, result])
    assert order == ["approval_requested", "approved"]


@pytest.mark.parametrize("approve", [True, False, None])
def test_exact_interactive_approval_precedes_effect_and_all_public_carriers_scrub(host_request, monkeypatch, approve):
    native = AsyncMock(return_value={"ok": True, "status": "succeeded", "error": "", "source_id": "native.synthetic"})
    monkeypatch.setattr("service.assistant.outbox.request", native)
    audits = []
    monkeypatch.setattr(loop, "audit", lambda event, **fields: audits.append((event, fields)))
    client = planned_client()
    result, events, order = run(client, approve=approve)
    cards = [event for event in events if event["type"] == "confirm"]
    assert len(cards) == 1
    assert "No attendees will be invited" in cards[0]["preview"]
    assert "Meeting with Morgan" in cards[0]["preview"]
    for token in S._meeting_capabilities:
        assert token not in json.dumps([client.requests, events, audits, result])
    assert native.await_count == (1 if approve else 0)
    if approve:
        assert order == ["approval_requested", "approved"]
        assert any("Private calendar block created:" in str(e) for e in events)
    else:
        assert not any(record["consumed"] for record in S._meeting_capabilities.values())


def test_mutating_visible_approval_args_does_not_change_reviewed_effect(host_request, monkeypatch):
    native = AsyncMock(return_value={"ok": True, "status": "succeeded", "error": "", "source_id": "native.synthetic"})
    monkeypatch.setattr("service.assistant.outbox.request", native)
    seen = []
    def mutate(event, approver):
        approver._actions[event["id"]]["args"]["title"] = "Changed during await"
        seen[0]["duration_min"] = 99
    result, events, _ = run(planned_client(capture=seen), mutate=mutate)
    assert native.await_count == 1
    payload = native.await_args.args[1]
    assert payload["title"] == "Meeting with Morgan" and payload["duration_min"] == 30


def test_context_change_after_approval_cannot_dispatch(host_request, monkeypatch):
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request", native)
    def mutate(event, approver):
        host_request.source.set({**host_request.source.get(), "source_id": "another-request"})
    result, events, _ = run(planned_client(), mutate=mutate)
    native.assert_not_awaited()
    assert any("invalid" in str(e) or "originating" in str(e) for e in events)


@pytest.mark.parametrize("base,managed", [("https://cloud.invalid", True), ("http://127.0.0.1:8000", False)])
def test_meeting_transport_requires_managed_loopback_before_model_or_action(host_request, monkeypatch, base, managed):
    client = Client([("commit_private_meeting", proposal_args())])
    client.base_url, client.managed = base, managed
    result, events, _ = run(client)
    assert not client.requests
    assert "local inference" in result


def test_direct_commit_refused_before_confirmation_or_effect(host_request):
    args = proposal_args()
    result, events, _ = run(Client([]), direct_calls=[("commit_private_meeting", args)])
    assert "direct dispatch refused" in result
    assert not any(event["type"] == "confirm" for event in events)


def test_capability_cannot_be_exfiltrated_by_other_tool(host_request, monkeypatch):
    args = proposal_args()
    run_tool = AsyncMock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr(loop, "run_tool", run_tool)
    client = Client([("write_file", {"path": "/private/tmp/fictional.txt", "text": args["capability_token"]})])
    result, events, _ = run(client, tools=["commit_private_meeting", "write_file"])
    run_tool.assert_not_awaited()
    assert args["capability_token"] not in json.dumps(events)
    assert not any(event["type"] == "confirm" for event in events)


def test_return_exception_and_entry_store_scrub_bare_capability(host_request, tmp_path):
    from service.safety.redaction import scrub, PLACEHOLDER
    from service.memory.store import SessionStore
    token = proposal_args()["capability_token"]
    assert scrub(token) == (PLACEHOLDER, 1)
    store = SessionStore(tmp_path / "synthetic.db")
    session = store.create_session()
    store.add_turn(session, "user", "Review " + token)
    store.add_turn(session, "assistant", token)
    assert token not in str(store._db.execute("SELECT content FROM turns").fetchall())
    assert token not in str(store._db.execute("SELECT text FROM memory_turn_fts").fetchall())
    async def raises():
        raise ValueError("broken " + token)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(loop._meeting_redacted_return(raises)())
    assert token not in str(caught.value) and caught.value.__suppress_context__


def test_pasted_transcript_capability_is_scrubbed_before_any_transport(host_request):
    token = proposal_args()["capability_token"]
    client = Client([])
    client.managed, client.base_url = False, "https://cloud.invalid"
    async def scenario():
        async def emit(event):
            assert token not in str(event)
        class Approver:
            async def confirm(self, action):
                raise AssertionError("read-only transport control")
        return await loop.run_agent(client, "synthetic-remote", [{"role": "user", "content": token}],
            emit, Approver(), tools=["calculate"], include_memory_context=False, max_steps=1)
    asyncio.run(scenario())
    assert token not in json.dumps(client.requests)


@pytest.mark.parametrize("name,prefix,effect", [
    ("commit_private_meeting", "Private calendar block created: ", "created"),
    ("save_knowledge", "Saved knowledge: ", "created"),
    ("edit_knowledge", "Updated knowledge: ", "updated"),
    ("delete_knowledge", "Deleted knowledge: ", "deleted"),
])
def test_new_effects_require_strict_receipt_prefix(name, prefix, effect):
    assert classify_tool_outcome(name, "Done").status == "failed"
    outcome = classify_tool_outcome(name, prefix + '{"id":"synthetic"}')
    assert outcome.status == "succeeded" and outcome.effect == effect


@pytest.mark.parametrize("mutation", ["title", "duration_min", "when_iso", "extra", "unknown_ref"])
def test_model_cannot_change_or_invent_host_proposal(host_request, monkeypatch, mutation):
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request", native)
    def change(args):
        if mutation == "title": args["title"] = "Another title"
        if mutation == "duration_min": args["duration_min"] = True
        if mutation == "when_iso": args["when_iso"] += "0"
        if mutation == "extra": args["extra"] = "field"
        if mutation == "unknown_ref": args["capability_token"] = "wisp_meeting_ref_invented"
    result, events, _ = run(planned_client(transform=change))
    native.assert_not_awaited()
    assert not any(e["type"] == "confirm" for e in events)


def test_model_supplied_raw_bearer_is_not_a_host_reference(host_request, monkeypatch):
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request", native)
    raw = proposal_args()
    result, events, _ = run(Client([("commit_private_meeting", raw)]))
    native.assert_not_awaited()
    assert not any(e["type"] == "confirm" for e in events)


def test_reference_from_prior_turn_is_inert_and_cannot_be_replayed(host_request, monkeypatch):
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request", native)
    seen = []
    run(planned_client(capture=seen), approve=False)
    result, events, _ = run(Client([("commit_private_meeting", seen[0])]))
    native.assert_not_awaited()
    assert not any(e["type"] == "confirm" for e in events)


def test_model_only_sees_inert_reference_even_when_it_encodes_it(host_request, monkeypatch):
    import base64
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request", native)
    client = planned_client()
    # The fake transport can transform only its actual model-facing transcript.
    async def stream(model, messages, **kwargs):
        client.requests.append(json.loads(json.dumps(messages)))
        if len(client.requests) == 1:
            name, args = client.calls[0]
            message = {"role": "assistant", "content": "", "tool_calls": [{"id": "plan", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        else:
            transcript = json.dumps(messages)
            message = {"role": "assistant", "content": base64.b64encode(transcript.encode()).decode() + "\n" + transcript[:len(transcript)//2] + "\n" + transcript[len(transcript)//2:]}
        yield {"kind": "final", "message": message}
    client.stream_events = stream
    result, events, _ = run(client)
    native.assert_not_awaited()
    text = json.dumps([client.requests, events, result])
    for token in S._meeting_capabilities:
        assert token not in text
        assert base64.b64encode(token.encode()).decode() not in text
        assert token[:len(token)//2] not in text
        assert token[len(token)//2:] not in text


@pytest.mark.parametrize("approve", [False, None])
def test_same_turn_reference_cannot_be_reused_after_denial_or_timeout(host_request, monkeypatch, approve):
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request",native)
    seen = []
    client = planned_client(capture=seen)
    client.calls.append(lambda messages:("commit_private_meeting",seen[0]))
    result,events,_ = run(client,approve=approve,timeout=.01)
    assert len([e for e in events if e["type"]=="confirm"]) == 1
    native.assert_not_awaited()


@pytest.mark.parametrize("field", ["session_id","source_id","quote"])
def test_context_drift_before_reference_resolution_never_offers_approval(host_request,monkeypatch,field):
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request",native)
    def change(args):
        host_request.source.set({**host_request.source.get(),field:"Changed originating context"})
    result,events,_ = run(planned_client(transform=change))
    assert not any(e["type"]=="confirm" for e in events)
    native.assert_not_awaited()


def test_reference_expiry_during_approval_does_not_dispatch(host_request,monkeypatch):
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request",native)
    def expire(event,approver):
        for record in S._meeting_capabilities.values(): record["expires"] = 0
    result,events,_ = run(planned_client(),mutate=expire)
    assert len([e for e in events if e["type"]=="confirm"]) == 1
    native.assert_not_awaited()
