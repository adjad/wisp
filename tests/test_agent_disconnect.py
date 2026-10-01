"""M-3: a client that goes away must not leave the backend working, or its books wrong.

The stream already cancelled the runner on a clean close, but: cancellation is a
BaseException, so none of the state-settling ran (a typed task or workflow stayed
"running" and the user's message was never saved); and the runner was started before
the body was read, so a client that never read it (or dropped it without closing it)
left a registry entry and a running turn behind.
"""
import asyncio
import gc
import json
import types
from unittest.mock import AsyncMock

import pytest

from service import idle, main


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main, "client", AsyncMock(), raising=False)
    state = types.SimpleNamespace(started=False, cancelled=False, finished=False)

    async def run_agent_waiting_for_approval(client, model, messages, emit, approver, **kw):
        # What the real loop does for a risky tool: ask, then wait for the person.
        state.started = True
        try:
            await emit({"type": "tool_call", "id": "call_0", "name": "send_email", "args": {}})
            approved = await approver.confirm({"id": "call_0", "tool": "send_email", "args": {},
                                               "reason": "sends something on your behalf"})
            await emit({"type": "text", "text": "approved" if approved else "denied"})
        except asyncio.CancelledError:
            state.cancelled = True
            raise
        state.finished = True
        return "done"
    monkeypatch.setattr(main, "run_agent", run_agent_waiting_for_approval, raising=False)
    yield state
    main.SESSIONS.clear()


async def start(prompt="delete the folder ~/Desktop/old-stuff", **body):
    response = await main.agent({"prompt": prompt, **body})
    return response.body_iterator


async def read_until(iterator, event_type):
    seen = []
    while True:
        event = json.loads((await asyncio.wait_for(iterator.__anext__(), 20))[6:])
        seen.append(event)
        if event["type"] == event_type:
            return seen


def turns(sid):
    return [(t["role"], t["content"]) for t in main.store.turns_from(sid, 0)]


def test_disconnect_while_an_approval_is_pending_cancels_cleans_up_and_saves_the_turn(isolated):
    async def scenario():
        iterator = await start()
        seen = await read_until(iterator, "confirm")
        sid = next(e["id"] for e in seen if e["type"] == "session")
        approver = next(iter(main.SESSIONS.values()))["approver"]
        assert approver.has_pending("call_0") and idle.foreground_busy()
        await iterator.aclose()                        # the app goes away mid-turn
        await asyncio.sleep(0.2)
        assert isolated.cancelled and not isolated.finished, "work must stop, not be approved by omission"
        assert not main.SESSIONS, "the registry entry must not outlive the stream"
        assert not approver.has_pending("call_0"), "the open approval must not linger"
        assert not idle.foreground_busy(), "background jobs must be released"
        saved = turns(sid)
        assert [role for role, _ in saved] == ["user", "assistant"], saved
        assert saved[0][1] == "delete the folder ~/Desktop/old-stuff", "the user's message was never saved before"
        assert "disconnected before it finished" in saved[1][1] and "Check any actions already reported" in saved[1][1]
    asyncio.run(scenario())


def _typed_task(monkeypatch, *, claimed):
    """A typed task that is mid-execution, as prepare_task_turn_async/execute_task leave it."""
    plan = types.SimpleNamespace(status="running", intent="message.send", claimed_calls=list(claimed),
                                 to_dict=lambda: {"status": plan.status})
    turn = types.SimpleNamespace(executable=True, response="", plan=plan, event="planned", trace=[])
    settled = []

    async def prepare(*a, **k):
        return turn

    async def execute_waiting(plan_, emit, approver, **k):
        await approver.confirm({"id": "task_call", "tool": "send_message", "args": {}, "reason": "sends"})
        return types.SimpleNamespace(finalize=True, response="", tool_calls=[], status="succeeded")
    monkeypatch.setattr("service.tasks.reply_engine.prepare_task_turn_async", prepare)
    monkeypatch.setattr(main, "execute_task", execute_waiting)
    monkeypatch.setattr(main, "finish_task",
                        lambda store, sid, plan_, *, status, result="": settled.append((status, result)))
    return plan, settled


@pytest.mark.parametrize("claimed,expect_unknown", [(["call_0"], True), ([], False)])
def test_a_running_typed_task_is_settled_not_left_running(monkeypatch, claimed, expect_unknown):
    plan, settled = _typed_task(monkeypatch, claimed=claimed)

    async def scenario():
        iterator = await start("text Mom I am outside")
        seen = await read_until(iterator, "confirm")
        sid = next(e["id"] for e in seen if e["type"] == "session")
        await iterator.aclose()
        await asyncio.sleep(0.2)
        assert [status for status, _ in settled] == ["failed"], "the running task must be settled"
        assert "disconnected" in settled[0][1]
        note = turns(sid)[-1][1]
        assert ("Sending was already attempted; its outcome is unknown" in note) is expect_unknown
    asyncio.run(scenario())


def test_a_real_running_workflow_is_settled_on_disconnect(isolated, monkeypatch):
    """Uses the real workflow planner and its persisted row: a delivery request creates
    a workflow, a stand-in executor waits for approval (the real one would, after
    resolving a recipient), and the app disconnects."""
    async def executor_waiting_for_approval(plan, emit, approver, **kw):
        isolated.started = True
        try:
            await approver.confirm({"id": "wf_call", "tool": "send_message", "args": {}, "reason": "sends"})
        except asyncio.CancelledError:
            isolated.cancelled = True
            raise
        return types.SimpleNamespace(finalize=True, response="", tool_calls=[], status="completed")
    monkeypatch.setattr("service.workflows.executor.execute_workflow", executor_waiting_for_approval)

    async def scenario():
        iterator = await start("text Mom a summary of my emails")
        seen = await read_until(iterator, "confirm")
        sid = next(e["id"] for e in seen if e["type"] == "session")
        rows = main.store._db.execute("SELECT status FROM workflows WHERE session_id=?", (sid,)).fetchall()
        assert [r["status"] for r in rows] == ["running"], "precondition: a workflow is mid-run"
        await iterator.aclose()
        await asyncio.sleep(0.2)
        rows = main.store._db.execute("SELECT status FROM workflows WHERE session_id=?", (sid,)).fetchall()
        assert rows and all(r["status"] != "running" for r in rows), \
            "a disconnect must not leave the workflow 'running' (it blocks the next delivery request)"
    asyncio.run(scenario())


def test_a_body_that_is_never_read_starts_nothing(isolated):
    async def scenario():
        iterator = await start()
        await asyncio.sleep(0.3)
        assert not isolated.started, "no turn may run for a client that never read the body"
        assert not main.SESSIONS and not idle.foreground_busy()
        del iterator
    asyncio.run(scenario())


def test_a_stream_dropped_without_closing_is_cleaned_up(isolated):
    async def scenario():
        iterator = await start()
        await read_until(iterator, "confirm")
        assert main.SESSIONS and isolated.started
        del iterator                                  # the connection dropped; nobody closed it
        for _ in range(5):
            gc.collect()
            await asyncio.sleep(0.1)
        assert isolated.cancelled and not main.SESSIONS and not idle.foreground_busy()
    asyncio.run(scenario())


def test_a_turn_that_finishes_normally_is_unchanged(monkeypatch, isolated):
    async def quick(client, model, messages, emit, approver, **kw):
        await emit({"type": "text", "text": "all done"})
        await emit({"type": "done"})
        return "all done"
    monkeypatch.setattr(main, "run_agent", quick, raising=False)

    async def scenario():
        iterator = await start("delete the folder ~/Desktop/old-stuff")
        seen = [json.loads(c[6:]) async for c in iterator]
        sid = next(e["id"] for e in seen if e["type"] == "session")
        assert [e["type"] for e in seen][-1] == "done"
        assert not main.SESSIONS
        assert not any("could not be completed" in text for _, text in turns(sid))
    asyncio.run(scenario())


def test_test_mode_saves_nothing_when_it_is_cancelled(isolated):
    async def scenario():
        iterator = await start(test_mode=True)
        seen = await read_until(iterator, "confirm")
        sid = next(e["id"] for e in seen if e["type"] == "session")
        await iterator.aclose()
        await asyncio.sleep(0.2)
        assert isolated.cancelled and not main.SESSIONS
        assert turns(sid) == [], "a stateless test run must leave no trace"
    asyncio.run(scenario())
