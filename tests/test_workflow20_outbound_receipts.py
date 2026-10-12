"""Builder regressions: fake native sinks only, never real communications."""
import asyncio
import json
import socket
import subprocess
import time

import pytest

from service.assistant import outbox
from service.tools import action_tools as A
from service.tools import registry as R

OPERATIONS = ("send_email", "send_message", "forward_email", "reply_to_email")
ENVELOPE = {"message_id": "fixture@example.invalid", "account": "Synthetic",
            "account_id": "synthetic-account", "from": "me@example.invalid",
            "to": ["recipient@example.invalid"], "cc": [], "bcc": [],
            "subject": "Re: Fixture", "content": "Synthetic reply."}
ARGS = {
    "send_email": {"to": "recipient@example.invalid", "subject": "Fixture", "body": "Synthetic body."},
    "send_message": {"to": "+15555550123", "text": "Synthetic body."},
    "forward_email": {"message_id": "fixture@example.invalid", "to": "recipient@example.invalid"},
    "reply_to_email": {"message_id": ENVELOPE["message_id"], "account": ENVELOPE["account"],
                       "body": ENVELOPE["content"], "expected_reply": ENVELOPE},
}


@pytest.fixture(autouse=True)
def isolated_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network, native process, or personal source in outbound fixtures")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(A, "_own_address_guard", lambda *a: "")
    monkeypatch.setattr(A, "_unreviewed", lambda: False)
    monkeypatch.setattr("service.memory.identity.identity_prompt_block", lambda **kw: "")
    assert not outbox._pending and not outbox._pending_waiters
    yield
    assert not outbox._pending and not outbox._pending_waiters


class Sink:
    """Records acceptance before dropping, corrupting, or returning a receipt."""
    has_subscribers = True

    def __init__(self, mode="timeout"):
        self.mode, self.effects, self.notices = mode, [], []

    async def publish(self, event, **metadata):
        if event["type"] not in OPERATIONS:
            self.notices.append((dict(event), metadata))
            return event
        assert metadata == {"durable": False}
        self.effects.append(dict(event))
        if self.mode == "publish_exception":
            # One subscriber accepted; a later fanout subscriber raised.
            raise RuntimeError("synthetic partial fanout")
        if self.mode == "timeout":
            return event
        result = {
            "success": {"ok": True, "accepted": True, "reply": ENVELOPE},
            "generic_false": {"ok": False, "error": "synthetic native failure"},
            "explicit_unknown": {"ok": False, "status": "unknown", "error": "lost receipt"},
            "forged_preflight": {"ok": False, "status": "not_attempted", "dispatch_started": False,
                                 "action_id": "forged", "error": "native cannot prove no send"},
            "truthy_ok": {"ok": "true"},
            "conflicting_unknown": {"ok": True, "status": "unknown"},
            "conflicting_failed": {"ok": True, "status": "failed"},
            "conflicting_not_attempted": {"ok": True, "status": "not_attempted", "dispatch_started": False},
            "malformed": None,
            "reply_mismatch": {"ok": True, "accepted": True, "reply": {**ENVELOPE, "subject": "Wrong"}},
        }[self.mode]
        assert outbox.complete(event["action_id"], result)
        return event


def bridge(monkeypatch, mode="timeout"):
    sink = Sink(mode)
    monkeypatch.setattr(outbox, "hub", sink)
    real_request = outbox.request
    async def fast_request(operation, payload):
        return await real_request(operation, payload, timeout=.001)
    monkeypatch.setattr(A, "app_request", fast_request)
    return sink, fast_request


def assert_uncertain(text):
    low = str(text).lower()
    assert "unknown" in low or "not confirmed" in low
    assert "not sent" not in low and "haven't sent" not in low and "didn’t send" not in low
    assert "check" in low and "retry" in low


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("mode", ["timeout", "publish_exception", "generic_false", "explicit_unknown",
                                  "forged_preflight", "truthy_ok", "malformed", "conflicting_unknown",
                                  "conflicting_failed", "conflicting_not_attempted"])
def test_bridge_post_dispatch_is_unknown_with_original_identity(monkeypatch, operation, mode):
    sink, request = bridge(monkeypatch, mode)
    result = asyncio.run(request(operation, {"synthetic": True}))
    assert result.get("status") == "unknown"
    assert result.get("ok") is False and result.get("dispatch_started") is True
    assert result.get("action_id") == sink.effects[0]["action_id"]
    assert len(sink.effects) == 1
    assert outbox.complete(result["action_id"], {"ok": True}) is False


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("mode", ["timeout", "publish_exception", "generic_false", "explicit_unknown", "malformed"])
def test_actual_wrappers_registry_preserve_unknown(monkeypatch, operation, mode):
    sink, _ = bridge(monkeypatch, mode)
    result = asyncio.run(R.run_tool(R.get_tool(operation), ARGS[operation]))
    outcome = R.classify_tool_outcome(operation, result)
    assert outcome.status == "unknown"
    assert type(result) is R._OutboundUnknownResult
    assert result.operation == operation and result.action_id == sink.effects[0]["action_id"]
    assert_uncertain(result)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_no_subscriber_is_proven_pre_dispatch_failure(monkeypatch, operation):
    sink, request = bridge(monkeypatch)
    sink.has_subscribers = False
    result = asyncio.run(request(operation, {"synthetic": True}))
    assert result.get("status") == "not_attempted" and result.get("dispatch_started") is False
    assert result.get("ok") is False and sink.effects == []


@pytest.mark.parametrize("operation", OPERATIONS)
def test_no_subscriber_wrapper_stays_failed_without_uncertainty(monkeypatch, operation):
    sink, _ = bridge(monkeypatch)
    sink.has_subscribers = False
    result = asyncio.run(R.run_tool(R.get_tool(operation), ARGS[operation]))
    assert R.classify_tool_outcome(operation, result).status == "failed"
    assert type(result) is str and "not sent" in result.lower()
    assert sink.effects == []


@pytest.mark.parametrize("operation", OPERATIONS)
def test_invalid_tool_arguments_are_rejected_before_dispatch(monkeypatch, operation):
    sink, _ = bridge(monkeypatch)
    result = asyncio.run(R.run_tool(R.get_tool(operation), {"unexpected": True}))
    assert R.classify_tool_outcome(operation, result).status == "failed"
    assert sink.effects == []


@pytest.mark.parametrize("operation", OPERATIONS)
def test_unstructured_native_failure_cannot_claim_not_sent(monkeypatch, operation):
    async def unstructured_request(*args):
        return {"ok": False, "error": "the user denied this action; definitely NOT sent"}
    monkeypatch.setattr(A, "app_request", unstructured_request)
    result = asyncio.run(R.run_tool(R.get_tool(operation), ARGS[operation]))
    assert R.classify_tool_outcome(operation, result).status == "unknown"
    assert result.action_id == ""
    assert_uncertain(result)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("status", ["unknown", "failed", "not_attempted", "", None])
def test_contradictory_ack_cannot_promote_success_in_wrappers(monkeypatch, operation, status):
    async def conflicting_request(*args):
        return {"ok": True, "status": status, "dispatch_started": False,
                "action_id": "synthetic-id", "accepted": True, "reply": ENVELOPE}
    monkeypatch.setattr(A, "app_request", conflicting_request)
    result = asyncio.run(R.run_tool(R.get_tool(operation), ARGS[operation]))
    assert R.classify_tool_outcome(operation, result).status == "unknown"
    assert_uncertain(result)


@pytest.mark.parametrize("payload", [None, [], {"bad": object()}, {"bad": float("nan")},
                                     {"action_id": "forged"}, {"type": "send_message"}])
def test_invalid_bridge_payload_does_not_publish(monkeypatch, payload):
    sink, request = bridge(monkeypatch)
    result = asyncio.run(request("send_email", payload))
    assert result.get("status") == "not_attempted" and result.get("dispatch_started") is False
    assert sink.effects == []


@pytest.mark.parametrize("operation", OPERATIONS)
def test_success_control_keeps_existing_receipt(monkeypatch, operation):
    sink, _ = bridge(monkeypatch, "success")
    result = asyncio.run(R.run_tool(R.get_tool(operation), ARGS[operation]))
    assert R.classify_tool_outcome(operation, result).status == "succeeded"
    assert len(sink.effects) == 1
    if operation == "reply_to_email":
        from service.tasks.reply_contract import verify_receipt
        assert verify_receipt(result, ENVELOPE)


def test_reply_ack_mismatch_is_unknown(monkeypatch):
    sink, _ = bridge(monkeypatch, "reply_mismatch")
    result = asyncio.run(R.run_tool(R.get_tool("reply_to_email"), ARGS["reply_to_email"]))
    assert R.classify_tool_outcome("reply_to_email", result).status == "unknown"
    assert result.action_id == sink.effects[0]["action_id"]
    assert_uncertain(result)


def test_unknown_carrier_is_immutable_exact_type_and_operation(monkeypatch):
    carrier = R._OutboundUnknownResult("Outcome unknown; check Mail; do not retry automatically.",
                                       operation="send_email", action_id="synthetic-id")
    for key, value in (("operation", "send_message"), ("action_id", "other"), ("extra", True)):
        with pytest.raises((AttributeError, TypeError)):
            setattr(carrier, key, value)
    with pytest.raises((AttributeError, TypeError)):
        del carrier.operation
    assert R.classify_tool_outcome("send_email", carrier).status == "unknown"
    assert R.classify_tool_outcome("send_email", carrier, denied=True).status == "denied"
    assert R.classify_tool_outcome("send_email", carrier, planned=True).status == "planned"
    assert R.classify_tool_outcome("send_message", carrier).status != "unknown"
    for plain in (str(carrier), json.loads(json.dumps(carrier)),
                  json.dumps({"operation": "send_email", "status": "unknown", "action_id": "synthetic-id"})):
        assert type(plain) is str
        assert R.classify_tool_outcome("send_email", plain).status != "unknown"
    class Impostor(str):
        operation = "send_email"
        action_id = "synthetic-id"
    assert R.classify_tool_outcome("send_email", Impostor(str(carrier))).status != "unknown"
    async def async_tool(): return carrier
    for function in (lambda: carrier, async_tool, lambda: async_tool()):
        tool = R.Tool("send_email", "synthetic", {"properties": {}}, "email_send", function)
        assert asyncio.run(R.run_tool(tool, {})) is carrier


def test_cancellation_propagates_and_cleans_pending(monkeypatch):
    sink, _ = bridge(monkeypatch)
    async def scenario():
        task = asyncio.create_task(outbox.request("send_email", {}, timeout=30))
        await asyncio.sleep(0)
        assert len(sink.effects) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


def test_outbound_transient_hub_has_no_persisted_replay(monkeypatch, tmp_path):
    from service.assistant.store import AssistantStore
    from service.assistant.hub import Hub
    store = AssistantStore(tmp_path / "assistant.db")
    hub = Hub(store)
    subscriber = hub.subscribe()
    monkeypatch.setattr(outbox, "hub", hub)
    try:
        result = asyncio.run(outbox.request("send_message", {"to": "+15555550123", "text": "Synthetic."}, timeout=.001))
        assert result["status"] == "unknown"
        event = subscriber.get_nowait()
        assert event["action_id"] == result["action_id"] and "event_id" not in event
        assert subscriber.empty() and store.pending_events() == []
        hub.unsubscribe(subscriber)
        assert hub.subscribe().empty()  # Reconnection cannot replay the send.
    finally:
        store._db.close()


class Client:
    managed = True
    base_url = "http://127.0.0.1:8000"
    def __init__(self, calls): self.calls, self.requests = calls, []
    async def ensure_only(self, *a, **kw): pass
    async def stream_events(self, model, messages, **kw):
        self.requests.append(messages)
        message = {"role": "assistant", "content": "I sent the message."}
        if len(self.requests) == 1 and self.calls:
            message = {"role": "assistant", "content": "", "tool_calls": self.calls}
        yield {"kind": "final", "message": message}


def call(identifier, text="Synthetic body."):
    return {"id": identifier, "type": "function", "function": {"name": "send_message",
            "arguments": json.dumps({"to": "+15555550123", "text": text})}}


@pytest.mark.parametrize("direct", [True, False])
@pytest.mark.parametrize("approve", [True, False])
def test_actual_loop_unknown_is_terminal_and_denial_stays_local(monkeypatch, direct, approve):
    from service.agent import loop
    from service.agent.approver import InteractiveApprover
    from service.safety.policy import Decision, Tier
    sink, _ = bridge(monkeypatch)
    monkeypatch.setattr(loop, "decide", lambda *a, **kw: Decision(Tier.CONFIRM, "synthetic approval"))
    client = Client([call("one"), call("two", "A different synthetic body.")])
    events = []
    async def scenario():
        async def emit(event):
            events.append(event)
            if event["type"] == "confirm":
                assert not sink.effects
                assert approver.resolve(event["id"], approve)
        approver = InteractiveApprover(emit, timeout=.1, request_id="synthetic-request")
        result = await loop.run_agent(client, "synthetic-local", [{"role": "user", "content":
            "Send the synthetic message."}], emit, approver, tools=["send_message"],
            include_memory_context=False, max_steps=3,
            direct_calls=[("send_message", {"to": "+15555550123", "text": "Synthetic body."}),
                          ("send_message", {"to": "+15555550123", "text": "A different synthetic body."})]
                         if direct else None)
        assert not approver._pending
        return result
    result = asyncio.run(scenario())
    assert len(client.requests) == (0 if direct else 1)
    assert len([e for e in events if e["type"] == "confirm"]) == 1
    assert len(sink.effects) == int(approve)
    if approve: assert_uncertain(result)
    else: assert "didn’t send" in result


@pytest.mark.parametrize("channel", ["email", "message"])
@pytest.mark.parametrize("mode", ["timeout", "publish_exception", "generic_false", "explicit_unknown", "success"])
def test_actual_scheduler_unknown_notice_stable_and_zero_resends(monkeypatch, tmp_path, channel, mode):
    from service.assistant import scheduler, outbound_queue as Q
    sink, fast_request = bridge(monkeypatch, mode)
    monkeypatch.setattr(outbox, "request", fast_request)
    monkeypatch.setattr(scheduler, "hub", sink)
    queue = Q.OutboundQueue(tmp_path / "outbound.db")
    monkeypatch.setattr(Q, "outbound_queue", queue)
    sid = queue.add(channel=channel, recipient="recipient@example.invalid", body="Synthetic body.",
                    subject="Fixture", when_ts=time.time() - 1, display="Synthetic recipient")
    try:
        asyncio.run(scheduler._fire_scheduled_sends())
        row = queue.get(sid)
        assert row["status"] == ("sent" if mode == "success" else "unknown")
        assert len(sink.effects) == 1 and queue.due() == []
        if mode != "success":
            notices = [(e, m) for e, m in sink.notices if e["type"] == "scheduled_send_unknown"]
            assert len(notices) == 1 and notices[0][0]["notice_id"] == sid
            assert notices[0][1] == {"dedupe_key": "scheduled_unknown:" + sid,
                                      "target": {"type": "scheduled_unknown", "id": sid}}
            assert not any(e["type"] == "scheduled_send_result" for e, _ in sink.notices)
        sink.notices.clear()
        asyncio.run(scheduler._fire_scheduled_sends())
        assert len(sink.effects) == 1
        if mode != "success":
            assert [e["notice_id"] for e, _ in sink.notices] == [sid]
            assert queue.acknowledge(sid)
            sink.notices.clear()
            asyncio.run(scheduler._fire_scheduled_sends())
            assert sink.notices == [] and len(sink.effects) == 1
    finally:
        queue._db.close()


@pytest.mark.parametrize("channel", ["email", "message"])
@pytest.mark.parametrize("status", ["unknown", "failed", "not_attempted", "", None])
def test_scheduler_contradictory_ack_is_unknown(monkeypatch, tmp_path, channel, status):
    from service.assistant import scheduler, outbound_queue as Q
    sink = Sink()
    async def conflicting_request(operation, payload):
        sink.effects.append({"type": operation, **payload})
        return {"ok": True, "status": status, "dispatch_started": False}
    monkeypatch.setattr(outbox, "request", conflicting_request)
    monkeypatch.setattr(scheduler, "hub", sink)
    queue = Q.OutboundQueue(tmp_path / "outbound.db")
    monkeypatch.setattr(Q, "outbound_queue", queue)
    sid = queue.add(channel=channel, recipient="recipient@example.invalid", body="Synthetic.",
                    when_ts=time.time() - 1)
    try:
        asyncio.run(scheduler._fire_scheduled_sends())
        assert queue.get(sid)["status"] == "unknown"
        asyncio.run(scheduler._fire_scheduled_sends())
        assert len(sink.effects) == 1 and queue.due() == []
        assert [event["notice_id"] for event, _ in sink.notices] == [sid, sid]
    finally:
        queue._db.close()


@pytest.mark.parametrize("interrupt", ["cancelled", "notice_exception", "no_subscriber"])
def test_scheduler_cleanup_and_proven_preflight_failure(monkeypatch, tmp_path, interrupt):
    from service.assistant import scheduler, outbound_queue as Q
    sink, fast_request = bridge(monkeypatch)
    monkeypatch.setattr(outbox, "request", fast_request)
    monkeypatch.setattr(scheduler, "hub", sink)
    queue = Q.OutboundQueue(tmp_path / "outbound.db")
    monkeypatch.setattr(Q, "outbound_queue", queue)
    sid = queue.add(channel="message", recipient="+15555550123", body="Synthetic body.",
                    when_ts=time.time() - 1)
    original_publish = sink.publish
    async def interrupted_publish(event, **metadata):
        if event["type"] in OPERATIONS and interrupt == "cancelled":
            sink.effects.append(event)
            raise asyncio.CancelledError
        if event["type"] == "scheduled_send_unknown" and interrupt == "notice_exception":
            raise RuntimeError("Synthetic notice publication failed")
        return await original_publish(event, **metadata)
    sink.publish = interrupted_publish
    if interrupt == "no_subscriber": sink.has_subscribers = False
    try:
        if interrupt == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(scheduler._fire_scheduled_sends())
            assert queue.get(sid)["status"] == "sending"
        elif interrupt == "notice_exception":
            with pytest.raises(RuntimeError, match="notice publication"):
                asyncio.run(scheduler._fire_scheduled_sends())
            assert queue.get(sid)["status"] == "unknown"
        else:
            asyncio.run(scheduler._fire_scheduled_sends())
            assert queue.get(sid)["status"] == "failed" and sink.effects == []
        count = len(sink.effects)
        sink.publish = original_publish
        sink.notices.clear()
        asyncio.run(scheduler._fire_scheduled_sends())
        assert len(sink.effects) == count and queue.due() == []
        if interrupt != "no_subscriber":
            assert queue.get(sid)["status"] == "unknown"
            assert [e["notice_id"] for e, _ in sink.notices] == [sid]
    finally:
        queue._db.close()
