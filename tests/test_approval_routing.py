"""An approval decision authorises or refuses a real action, so it must be strict
(H-4) and must reach exactly the request that asked (H-5).

H-4: ``bool("false")`` is True, so a string, number or null in ``approved`` was
coerced into an APPROVAL; missing fields were an unhandled 500.
H-5: action ids are not unique across overlapping requests on one session (a local
model may emit "call_0" every time, and the calendar batch used a constant id), and
the endpoint resolved the first request that held the id, so answering one card could
approve another request's pending action.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from service import main
from service.agent.approver import InteractiveApprover


@pytest.fixture(autouse=True)
def clean_sessions():
    main.SESSIONS.clear()
    yield
    main.SESSIONS.clear()


def register(sid, events=None):
    """An in-flight request, registered exactly as /agent registers one."""
    req_id = uuid.uuid4().hex
    sink = events if events is not None else []

    async def emit(ev):
        sink.append(ev)
    approver = InteractiveApprover(emit, timeout=30, request_id=req_id)
    main.SESSIONS[req_id] = {"sid": sid, "queue": None, "approver": approver}
    return req_id, approver, sink


def body(**kw):
    base = {"session_id": "s1", "action_id": "call_0", "approved": True}
    base.update(kw)
    return base


# ------------------------------------------------------------------- H-4

@pytest.mark.parametrize("approved", ["false", "true", "False", "no", "", 1, 0, 2, None, [], {}, 0.0])
def test_a_non_boolean_decision_is_rejected_never_coerced(approved):
    async def scenario():
        _, approver, _ = register("s1")
        task = asyncio.create_task(approver.confirm({"id": "call_0", "tool": "send_email", "args": {}}))
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as error:
            await main.approve(body(approved=approved))
        assert error.value.status_code == 422
        assert approver.has_pending("call_0"), "a malformed decision must not resolve anything"
        approver.resolve("call_0", False, "once")
        assert await task is False
    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["session_id", "action_id", "approved"])
def test_missing_fields_are_a_422_not_a_500(missing):
    payload = body()
    payload.pop(missing)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.approve(payload))
    assert error.value.status_code == 422


@pytest.mark.parametrize("field,value", [("session_id", ""), ("action_id", ""), ("session_id", 7),
                                         ("action_id", None), ("scope", 5), ("scope", ["once"]),
                                         ("request_id", 12), ("request_id", "")])
def test_other_malformed_fields_are_rejected(field, value):
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.approve(body(**{field: value})))
    assert error.value.status_code == 422


def test_over_http_a_string_false_is_422_and_does_not_approve():
    async def hold():
        _, approver, _ = register("s1")
        return approver
    approver = asyncio.run(hold())
    response = TestClient(main.app).post("/agent/approve", json=body(approved="false"))
    assert response.status_code == 422
    assert not approver._pending  # nothing was ever pending or resolved


@pytest.mark.parametrize("approved", [True, False])
def test_real_booleans_resolve_with_exactly_that_value(approved):
    async def scenario():
        _, approver, _ = register("s1")
        task = asyncio.create_task(approver.confirm({"id": "call_0", "tool": "send_email", "args": {}}))
        await asyncio.sleep(0)
        result = await main.approve(body(approved=approved))
        assert result["ok"] is True
        assert await task is approved
    asyncio.run(scenario())


# ------------------------------------------------------------------- H-5

def test_confirm_and_timeout_events_carry_the_request_id():
    async def scenario():
        req_id, approver, events = register("s1")
        approver._timeout = 0.05
        assert await approver.confirm({"id": "call_0", "tool": "run_shell", "args": {}}) is False
        return req_id, events
    req_id, events = asyncio.run(scenario())
    assert [e["request_id"] for e in events if e["type"] in {"confirm", "confirm_timeout"}] == [req_id, req_id]


def test_overlapping_requests_with_the_same_action_id_are_answered_independently():
    """The reported bug: two same-session requests, both with id 'batch_calendar'."""
    async def scenario():
        a_id, a, _ = register("s1")
        b_id, b, _ = register("s1")
        ta = asyncio.create_task(a.confirm({"id": "call_0", "tool": "cancel_event", "args": {"t": "A"}}))
        tb = asyncio.create_task(b.confirm({"id": "call_0", "tool": "cancel_event", "args": {"t": "B"}}))
        await asyncio.sleep(0)
        # Approve ONLY request B.
        assert (await main.approve(body(request_id=b_id, approved=True)))["ok"] is True
        assert await tb is True
        assert a.has_pending("call_0"), "answering B must never touch A's pending action"
        assert not ta.done()
        # Now deny A.
        assert (await main.approve(body(request_id=a_id, approved=False)))["ok"] is True
        assert await ta is False
    asyncio.run(scenario())


def test_a_request_id_for_another_session_or_request_is_refused():
    async def scenario():
        a_id, a, _ = register("s1")
        ta = asyncio.create_task(a.confirm({"id": "call_0", "tool": "send_email", "args": {}}))
        await asyncio.sleep(0)
        assert (await main.approve(body(session_id="other", request_id=a_id)))["ok"] is False
        assert (await main.approve(body(request_id="does-not-exist")))["ok"] is False
        assert a.has_pending("call_0")
        a.resolve("call_0", False, "once")
        await ta
    asyncio.run(scenario())


def test_an_old_client_without_a_request_id_is_refused_when_the_id_is_ambiguous():
    async def scenario():
        _, a, _ = register("s1")
        _, b, _ = register("s1")
        ta = asyncio.create_task(a.confirm({"id": "call_0", "tool": "cancel_event", "args": {}}))
        tb = asyncio.create_task(b.confirm({"id": "call_0", "tool": "cancel_event", "args": {}}))
        await asyncio.sleep(0)
        result = await main.approve(body(approved=True))
        assert result["ok"] is False and "more than one request" in result["error"]
        assert a.has_pending("call_0") and b.has_pending("call_0"), "neither may be approved by guesswork"
        a.resolve("call_0", False, "once")
        b.resolve("call_0", False, "once")
        await asyncio.gather(ta, tb)
    asyncio.run(scenario())


def test_an_old_client_still_works_when_the_id_is_unambiguous():
    async def scenario():
        _, a, _ = register("s1")
        ta = asyncio.create_task(a.confirm({"id": "call_0", "tool": "send_email", "args": {}}))
        await asyncio.sleep(0)
        assert (await main.approve(body()))["ok"] is True
        assert await ta is True
    asyncio.run(scenario())


def test_the_calendar_batch_uses_a_unique_action_id_each_time():
    import inspect
    from service.agent import loop
    source = inspect.getsource(loop)
    assert '"id": "batch_calendar"' not in source
    assert 'f"batch_calendar_{uuid.uuid4().hex[:8]}"' in source


def test_the_real_agent_handler_stamps_its_approver_with_the_request_id(monkeypatch):
    """/agent must register the approver under a request id AND tell the approver,
    so every confirm card it emits can be answered to exactly that request."""
    import types

    created = []

    class Recorder:
        request_id = None

        def __init__(self, emit):
            created.append(self)

        async def confirm(self, action):
            return False

    async def instant_run(*args, **kwargs):
        return ""
    monkeypatch.setattr(main, "InteractiveApprover", Recorder)
    monkeypatch.setattr(main, "run_agent", instant_run, raising=False)
    monkeypatch.setattr(main, "client", types.SimpleNamespace(), raising=False)

    async def scenario():
        response = await main.agent({"prompt": "hello there", "test_mode": True})
        # The request registers when its body is first consumed, not before.
        await response.body_iterator.__anext__()
        registered = list(main.SESSIONS)
        await response.body_iterator.aclose()
        return registered
    registered = asyncio.run(scenario())
    assert len(created) == 1 and len(registered) == 1
    assert created[0].request_id == registered[0], "the approver must carry the registry's request id"
