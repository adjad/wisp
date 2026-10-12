"""Executor draft binding: actual host preparation, synthetic stores and sinks only."""
import asyncio
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

from service.memory.store import SessionStore
from service.safety.policy import Decision, Tier
from service.tools import action_tools as A, registry as R
from service.workflows import executor as E
from service.workflows.models import WorkflowPlan

PHONE = "+15555550123"
OTHER = "+15555550456"
BODY = "Synthetic approved body."
PUBLISHER = "PRIVATE_PUBLISHER_SENTINEL synthetic article."


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No Contacts/native/network/model/process effects in executor draft tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(A, "_resolve_recipient", forbidden)
    monkeypatch.setattr(A, "app_request", forbidden)
    monkeypatch.setattr(E, "decide", lambda *a, **kw: Decision(Tier.ALLOW, "synthetic"))


@pytest.fixture
def execution(tmp_path):
    stores = []
    def make(*, news=False, recipient=PHONE, body=BODY):
        store = SessionStore(tmp_path / f"sessions-{len(stores)}.db")
        stores.append(store)
        sid = store.create_session()
        plan = WorkflowPlan(recipient=recipient, channel="messages", delivery="draft",
                            artifact_text=body, artifact_provenance="verified_tool_receipt", status="running")
        if news:
            display = R.DisplayOnlyToolResult(body, model_text="Synthetic publisher metadata.", artifact_kind="news")
            idx = store.add_turn(sid, "assistant", display)
            artifact = store.display_artifact(sid, idx)
            plan.artifact_provenance = ""
            plan.news_artifact_provenance = artifact.provenance
        store.save_workflow(sid, plan.to_dict())
        state = SimpleNamespace(store=store, sid=sid, plan=plan, events=[], approvals=[],
                                effects=[], receipts=[], allow=True, on_event=None)
        async def emit(event):
            state.events.append(event)
            if state.on_event:
                await state.on_event(event)
        async def confirm(action):
            state.approvals.append(action)
            return state.allow
        state.emit, state.approver = emit, SimpleNamespace(confirm=confirm)
        return state
    yield make
    for store in stores:
        store._db.close()


async def execute(state, **kwargs):
    return await E.execute_workflow(state.plan, state.emit, state.approver,
                                    store=state.store, session_id=state.sid, **kwargs)


def cards(state):
    return [e for e in state.events if e.get("type") == "message_draft"]


def draft_fake(monkeypatch, state, mode="real"):
    original = R.get_tool("draft_message")
    async def prepare(**kwargs):
        state.effects.append(dict(kwargs))
        if mode == "prose": return "Message draft prepared in Wisp for fixture — nothing has been sent."
        if mode == "json": return json.dumps({"draftorigin": "host", "resolved_to": PHONE, "prepared_text": BODY})
        if mode == "display": return R.DisplayOnlyToolResult("Private display", model_text="Message draft prepared in Wisp.")
        if mode == "unknown": return R._OutboundUnknownResult("Message outcome unknown.", operation="send_message", action_id="synthetic")
        receipt = await A.draft_message(to=OTHER if mode == "recipient" else kwargs["to"],
                                        text="Different prepared body." if mode == "body" else kwargs["text"])
        state.receipts.append(receipt)
        if mode == "origin": object.__setattr__(receipt, "_draftorigin", object())
        if mode == "malformed": object.__setattr__(receipt, "_resolved_to", None)
        if mode == "subclass":
            class Lookalike(R._PreparedMessageDraft): pass
            return str.__new__(Lookalike, str(receipt))
        return receipt
    monkeypatch.setitem(R.REGISTRY, "draft_message", R.Tool(
        "draft_message", "Synthetic real preparation", original.parameters, "messages_draft", prepare))


@pytest.mark.parametrize("news", [False, True])
@pytest.mark.parametrize("mismatch", ["recipient", "body"])
def test_genuine_mismatched_receipt_cannot_complete_or_emit(monkeypatch, execution, news, mismatch):
    state = execution(news=news)
    draft_fake(monkeypatch, state, mismatch)
    result = asyncio.run(execute(state))
    assert len(state.effects) == 1 and R._valid_message_draft(state.receipts[0])
    assert state.effects == [state.approvals[0]["args"]]
    assert result.status == "failed" and cards(state) == []


@pytest.mark.parametrize("news", [False, True])
@pytest.mark.parametrize("invalidate", ["origin", "recipient", "body"])
def test_receipt_revalidated_after_tool_result_before_card(monkeypatch, execution, news, invalidate):
    state = execution(news=news)
    draft_fake(monkeypatch, state)
    async def event(event):
        if event.get("type") == "tool_result" and event.get("name") == "draft_message":
            receipt = state.receipts[-1]
            field, value = {"origin": ("_draftorigin", object()), "recipient": ("_resolved_to", OTHER),
                            "body": ("_prepared_text", "Changed after capture.")}[invalidate]
            object.__setattr__(receipt, field, value)
    state.on_event = event
    result = asyncio.run(execute(state))
    assert result.status == "failed" and not cards(state)
    assert len(state.effects) == 1


@pytest.mark.parametrize("news", [False, True])
def test_successful_receipt_card_matches_approved_canonical_args(monkeypatch, execution, news):
    state = execution(news=news, body=PUBLISHER if news else r"  Hello\nworld\twith quotes.  ")
    draft_fake(monkeypatch, state)
    result = asyncio.run(execute(state))
    assert result.status == "completed" and len(state.effects) == len(state.approvals) == 1
    receipt = state.receipts[0]
    assert cards(state) == [{"type": "message_draft", "to": receipt.resolved_to, "text": receipt.prepared_text}]
    assert state.effects[0] == state.approvals[0]["args"] == {"to": receipt.resolved_to, "text": receipt.prepared_text}
    assert set(result.tool_results[-1]) == {"id", "name", "result", "status"}
    assert result.tool_results[-1]["id"] == result.tool_calls[-1]["id"]
    if news:
        assert type(result.response) is R.DisplayOnlyToolResult
        assert PUBLISHER not in result.response.model_text
        assert PUBLISHER not in json.dumps(result.tool_results)
        assert result.tool_results[-1]["result"] == "Message draft prepared in Wisp."
    else:
        assert receipt.prepared_text == "Hello\nworld\twith quotes."


@pytest.mark.parametrize("news", [False, True])
@pytest.mark.parametrize("mode", ["prose", "json", "display", "unknown", "origin", "malformed", "subclass"])
def test_untrusted_or_invalid_results_never_create_card(monkeypatch, execution, news, mode):
    state = execution(news=news)
    draft_fake(monkeypatch, state, mode)
    result = asyncio.run(execute(state))
    assert result.status == "failed" and not cards(state)
    assert len(state.effects) == 1


@pytest.mark.parametrize("news", [False, True])
@pytest.mark.parametrize("gate", ["policy", "user", "planned", "schema", "missing", "unavailable",
                                  "stale", "used_claim", "claim_failed"])
def test_existing_gates_never_run_preparation_or_emit_card(monkeypatch, execution, news, gate):
    state = execution(news=news)
    draft_fake(monkeypatch, state)
    kwargs = {}
    if gate == "policy": monkeypatch.setattr(E, "decide", lambda *a, **kw: Decision(Tier.DENY, "Synthetic policy denial"))
    elif gate == "user": state.allow = False
    elif gate == "planned": kwargs["test_mode"] = True
    elif gate == "schema": monkeypatch.setattr(R.get_tool("draft_message"), "parameters", {"properties": {}, "required": ["missing"]})
    elif gate == "missing": monkeypatch.delitem(R.REGISTRY, "draft_message")
    elif gate == "unavailable": monkeypatch.setattr(R.get_tool("draft_message"), "unavailable_reason", "Synthetic unavailable tool.")
    elif gate == "stale":
        changed = state.plan.to_dict(); changed["revision"] += 1
        state.store.save_workflow(state.sid, changed)
    elif gate == "used_claim":
        assert state.store.claim_workflow_effect(state.sid, state.plan.id, f"workflow_effect:{state.plan.id}", revision=state.plan.revision)
    elif gate == "claim_failed": monkeypatch.setattr(state.store, "claim_workflow_effect", lambda *a, **kw: False)
    result = asyncio.run(execute(state, **kwargs))
    assert result.status != "completed" and not cards(state) and not state.effects
    if gate in {"policy", "planned", "schema", "missing", "stale", "used_claim"}:
        assert not state.approvals


@pytest.mark.parametrize("change", ["failed_source", "source_revision", "approval_revision"])
def test_failed_source_or_revision_change_blocks_final_draft(monkeypatch, execution, change):
    state = execution()
    draft_fake(monkeypatch, state)
    state.plan.artifact_text = state.plan.artifact_provenance = ""
    state.plan.sources = ["email"]
    state.store.save_workflow(state.sid, state.plan.to_dict())
    def revise():
        changed = state.plan.to_dict(); changed["revision"] += 1
        state.store.save_workflow(state.sid, changed)
    async def read(**kwargs):
        if change == "failed_source": return "(error: Synthetic source unavailable)"
        if change == "source_revision": revise()
        return "Synthetic scoped source body."
    monkeypatch.setitem(R.REGISTRY, "summarize_emails", R.Tool(
        "summarize_emails", "Synthetic read", {"properties": {}}, "assistant_read", read))
    if change == "approval_revision":
        async def approve(action):
            state.approvals.append(action); revise(); return True
        state.approver = SimpleNamespace(confirm=approve)
    result = asyncio.run(execute(state))
    assert result.status == "failed" and not state.effects and not cards(state)
    assert len(state.approvals) == int(change == "approval_revision")


def test_mismatch_consumes_existing_claim_without_retry(monkeypatch, execution):
    state = execution()
    draft_fake(monkeypatch, state, "recipient")
    first = asyncio.run(execute(state))
    second = asyncio.run(execute(state))
    assert first.status == second.status == "failed" and not cards(state)
    assert len(state.effects) == len(state.approvals) == 1
    assert state.store.workflow_effect_claimed(state.plan.id)


def test_read_receipt_cannot_supply_final_draft_authority(monkeypatch, execution):
    state = execution()
    draft_fake(monkeypatch, state, "prose")
    ambient = asyncio.run(A.draft_message(OTHER, "Unrelated read receipt."))
    state.plan.artifact_text = state.plan.artifact_provenance = ""
    state.plan.sources = ["email"]
    state.store.save_workflow(state.sid, state.plan.to_dict())
    async def read(**kwargs): return ambient
    monkeypatch.setitem(R.REGISTRY, "summarize_emails", R.Tool(
        "summarize_emails", "Synthetic read", {"properties": {}}, "assistant_read", read))
    result = asyncio.run(execute(state))
    assert result.status == "failed" and not cards(state) and len(state.effects) == 1
    assert result.tool_results[0]["name"] == "summarize_emails"
    assert type(result.tool_results[0]["result"]) is str


@pytest.mark.parametrize("news", [False, True])
def test_missing_host_receipt_cannot_be_rescued_by_success_classification(monkeypatch, execution, news):
    state = execution(news=news)
    draft_fake(monkeypatch, state, "prose")
    original = E.classify_tool_outcome
    def faulty_classifier(name, result, **kwargs):
        return SimpleNamespace(status="succeeded") if name == "draft_message" else original(name, result, **kwargs)
    monkeypatch.setattr(E, "classify_tool_outcome", faulty_classifier)
    result = asyncio.run(execute(state))
    assert result.status == "failed" and not cards(state)
    assert len(state.effects) == 1
    assert result.tool_results[-1]["status"] == "failed"


def test_reentrant_workflows_keep_receipts_and_call_ids_separate(monkeypatch, execution):
    first = execution(news=True, body=PUBLISHER)
    second = execution(recipient=OTHER, body="Second workflow body.")
    original = R.get_tool("draft_message")
    async def prepare(**kwargs):
        await asyncio.sleep(0)
        state = first if kwargs["to"] == PHONE else second
        state.effects.append(dict(kwargs))
        receipt = await A.draft_message(**kwargs)
        state.receipts.append(receipt)
        return receipt
    monkeypatch.setitem(R.REGISTRY, "draft_message", R.Tool(
        "draft_message", "Synthetic preparation", original.parameters, "messages_draft", prepare))
    nested = []
    async def event(event):
        if event.get("type") == "tool_result": nested.append(await execute(second))
    first.on_event = event
    result = asyncio.run(execute(first))
    assert result.status == nested[0].status == "completed"
    for state in (first, second):
        assert cards(state) == [{"type": "message_draft", "to": state.plan.recipient,
                                "text": state.plan.artifact_text}]
        assert len(state.effects) == len(state.approvals) == 1
        assert state.approvals[0]["id"].startswith(f"workflow_{state.plan.id}_")
    assert first.receipts[0] is not second.receipts[0]


def test_concurrent_execution_and_prior_success_do_not_reuse_authority(monkeypatch, execution):
    states = [execution(recipient=PHONE, body="First body."), execution(recipient=OTHER, body="Second body.")]
    original = R.get_tool("draft_message")
    async def prepare(**kwargs):
        await asyncio.sleep(0)
        for state in states:
            if kwargs["to"] == state.plan.recipient:
                state.effects.append(dict(kwargs))
        return await A.draft_message(**kwargs)
    monkeypatch.setitem(R.REGISTRY, "draft_message", R.Tool(
        "draft_message", "Synthetic preparation", original.parameters, "messages_draft", prepare))
    async def scenario(): return await asyncio.gather(*(execute(state) for state in states))
    assert all(result.status == "completed" for result in asyncio.run(scenario()))
    for state in states:
        assert cards(state) == [{"type": "message_draft", "to": state.plan.recipient, "text": state.plan.artifact_text}]
    failed = execution()
    draft_fake(monkeypatch, failed, "prose")
    assert asyncio.run(execute(failed)).status == "failed" and not cards(failed)


@pytest.mark.parametrize("news", [False, True])
def test_real_send_unknown_remains_unknown_without_retry_or_card(monkeypatch, execution, news):
    state = execution(news=news)
    state.plan.delivery = "send"
    state.store.save_workflow(state.sid, state.plan.to_dict())
    accepted = []
    async def sink(operation, payload):
        accepted.append((operation, dict(payload)))
        return {"ok": False, "status": "unknown", "dispatch_started": True, "action_id": "synthetic-send"}
    monkeypatch.setattr(A, "app_request", sink)
    monkeypatch.setattr(A, "_own_address_guard", lambda *a: "")
    result = asyncio.run(execute(state))
    assert result.status == "unknown" and not cards(state) and len(accepted) == 1
    assert accepted[0] == ("send_message", state.approvals[0]["args"])
    assert asyncio.run(execute(state)).status == "failed" and len(accepted) == 1


@pytest.mark.parametrize("delivery,channel,effect,receipt", [
    ("send", "messages", "send_message", "Message sent to +15555550123."),
    ("send", "email", "send_email", "Email sent to fixture@synthetic.invalid."),
    ("draft", "email", "draft_email", "Draft opened in Mail to fixture@synthetic.invalid."),
    ("scheduled", "messages", "schedule_send", "Scheduled: synthetic fixture."),
])
def test_non_draft_effect_contract_remains_unchanged(monkeypatch, execution, delivery, channel, effect, receipt):
    state = execution(recipient="fixture@synthetic.invalid" if channel == "email" else PHONE)
    state.plan.delivery, state.plan.channel = delivery, channel
    state.plan.when = "2031-01-01T12:00:00+00:00"
    state.store.save_workflow(state.sid, state.plan.to_dict())
    original = R.get_tool(effect)
    async def sink(**kwargs):
        state.effects.append(dict(kwargs)); return receipt
    monkeypatch.setitem(R.REGISTRY, effect, R.Tool(effect, "Synthetic sink", original.parameters, original.category, sink))
    result = asyncio.run(execute(state))
    assert result.status == "completed" and not cards(state)
    assert state.effects == [state.approvals[0]["args"]]
    assert result.tool_results[-1]["status"] == "succeeded"
