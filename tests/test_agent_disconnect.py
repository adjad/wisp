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
import socket
import subprocess
import types
from unittest.mock import AsyncMock

import pytest

from service import idle, main


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main, "client", AsyncMock(), raising=False)
    monkeypatch.setattr(main, "ensure_omlx", AsyncMock())
    monkeypatch.setattr(main, "maybe_summarize", AsyncMock())
    def forbidden(*args, **kwargs):
        raise AssertionError("disconnect fixtures must not open sockets or native processes")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    state = types.SimpleNamespace(started=False, cancelled=False, finished=False)

    async def run_agent_waiting_for_approval(client, model, messages, emit, approver, **kw):
        # What the real loop does for a risky tool: ask, then wait for the person.
        state.started = True
        state.runner = asyncio.current_task()
        state.approver = approver
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
@pytest.mark.parametrize("transport", ["iterator", "asgi"])
def test_a_running_typed_task_is_settled_not_left_running(monkeypatch, claimed, expect_unknown, transport):
    plan, settled = _typed_task(monkeypatch, claimed=claimed)

    async def scenario():
        if transport == "asgi":
            seen = await serve_synthetic(await main.agent({"prompt": "text Mom I am outside"}))
        else:
            iterator = await start("text Mom I am outside")
            seen = await read_until(iterator, "confirm")
            await iterator.aclose()
        sid = next(e["id"] for e in seen if e["type"] == "session")
        await asyncio.sleep(0.2)
        assert [status for status, _ in settled] == ["failed"], "the running task must be settled"
        assert "disconnected" in settled[0][1]
        note = turns(sid)[-1][1]
        assert ("Sending was already attempted; its outcome is unknown" in note) is expect_unknown
    asyncio.run(scenario())


@pytest.mark.parametrize("transport", ["iterator", "asgi"])
def test_a_real_running_workflow_is_settled_on_disconnect(isolated, monkeypatch, transport):
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
        def check_running(seen):
            sid = next(e["id"] for e in seen if e["type"] == "session")
            rows = main.store._db.execute("SELECT status FROM workflows WHERE session_id=?", (sid,)).fetchall()
            assert [r["status"] for r in rows] == ["running"], "precondition: a workflow is mid-run"
        if transport == "asgi":
            seen = await serve_synthetic(await main.agent({"prompt": "text Mom a summary of my emails"}),
                                         on_confirm=check_running)
        else:
            iterator = await start("text Mom a summary of my emails")
            seen = await read_until(iterator, "confirm")
            check_running(seen)
            await iterator.aclose()
        sid = next(e["id"] for e in seen if e["type"] == "session")
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


@pytest.mark.parametrize("transport", ["iterator", "asgi"])
def test_test_mode_saves_nothing_when_it_is_cancelled(isolated, transport):
    async def scenario():
        if transport == "asgi":
            seen = await serve_synthetic(await main.agent({"prompt": "delete the folder ~/Desktop/old-stuff",
                                                          "test_mode": True}))
        else:
            iterator = await start(test_mode=True)
            seen = await read_until(iterator, "confirm")
            await iterator.aclose()
        sid = next(e["id"] for e in seen if e["type"] == "session")
        await asyncio.sleep(0.2)
        assert isolated.cancelled and not main.SESSIONS
        assert turns(sid) == [], "a stateless test run must leave no trace"
    asyncio.run(scenario())


async def serve_synthetic(response, *, disconnect=True, on_confirm=None):
    """Exercise Starlette's actual disconnect scope without a socket or lifespan."""
    confirmed = asyncio.Event()
    seen = []

    async def send(message):
        body = message.get("body", b"")
        if body:
            event = json.loads(body[6:])
            seen.append(event)
            if event["type"] == "confirm":
                if on_confirm:
                    on_confirm(seen)
                confirmed.set()

    async def receive():
        if disconnect:
            await confirmed.wait()
            return {"type": "http.disconnect"}
        await asyncio.Future()

    await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}},
                                    receive, send), 5)
    return seen


@pytest.mark.parametrize("repeat", range(3))
def test_actual_asgi_disconnect_settles_and_unregisters(isolated, repeat):
    async def scenario():
        response = await main.agent({"prompt": "delete the folder ~/Desktop/old-stuff"})
        seen = await serve_synthetic(response)
        sid = next(e["id"] for e in seen if e["type"] == "session")
        assert isolated.cancelled and not isolated.finished
        assert not main.SESSIONS, "ASGI cancellation must not skip registry removal"
        assert isolated.runner.done() and not isolated.approver.has_pending("call_0")
        assert not idle.foreground_busy()
        saved = turns(sid)
        assert [role for role, _ in saved] == ["user", "assistant"]
        assert "disconnected before it finished" in saved[-1][1]
    asyncio.run(scenario())


def test_asgi_send_failure_closes_a_retained_response(isolated):
    from starlette.requests import ClientDisconnect

    async def scenario():
        response = await main.agent({"prompt": "delete the folder ~/Desktop/old-stuff"})
        seen = []
        async def send(message):
            if message.get("body"):
                event = json.loads(message["body"][6:])
                seen.append(event)
                if event["type"] == "confirm":
                    raise OSError("synthetic closed transport")
        async def receive():
            raise AssertionError("ASGI 2.4 must use send failure")
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert response.body_iterator is not None  # retained, so GC cannot hide a leak
        assert isolated.runner.done() and isolated.cancelled and not isolated.finished
        assert not isolated.approver.has_pending("call_0")
        assert not main.SESSIONS and not idle.foreground_busy()
        sid = next(e["id"] for e in seen if e["type"] == "session")
        assert "disconnected before it finished" in turns(sid)[-1][1]
    asyncio.run(scenario())


@pytest.mark.parametrize("release_cleanup", [True, False])
def test_repeated_cancellation_joins_runner_and_bounds_client_cleanup(monkeypatch, isolated, release_cleanup):
    async def scenario():
        closing = asyncio.Event()
        release = asyncio.Event()
        closed = asyncio.Event()
        async def close_fallback(self):
            closing.set()
            try:
                await release.wait()
            finally:
                closed.set()
        monkeypatch.setattr(main.TurnInferenceClient, "close_fallback", close_fallback)
        response = await main.agent({"prompt": "delete the folder ~/Desktop/old-stuff"})
        confirmed = asyncio.Event()
        async def send(message):
            if message.get("body") and json.loads(message["body"][6:])["type"] == "confirm":
                confirmed.set()
                await asyncio.Future()
        async def receive():
            raise AssertionError("ASGI 2.4 must use send failure")
        task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
        await asyncio.wait_for(confirmed.wait(), 2)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        if release_cleanup:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert closed.is_set() and isolated.runner.done()
        assert not isolated.approver.has_pending("call_0")
        assert not main.SESSIONS and not idle.foreground_busy()
        assert isolated.cancelled and not isolated.finished
    asyncio.run(scenario())


def test_actual_asgi_normal_completion_has_no_interrupted_turn(monkeypatch, isolated):
    async def quick(client, model, messages, emit, approver, **kw):
        await emit({"type": "text", "text": "all done"})
        await emit({"type": "done"})
        return "all done"
    monkeypatch.setattr(main, "run_agent", quick)
    async def scenario():
        response = await main.agent({"prompt": "delete the folder ~/Desktop/old-stuff"})
        seen = await serve_synthetic(response, disconnect=False)
        sid = next(e["id"] for e in seen if e["type"] == "session")
        assert seen[-1]["type"] == "done"
        assert turns(sid)[-1] == ("assistant", "all done")
        assert not main.SESSIONS and not idle.foreground_busy()
    asyncio.run(scenario())


@pytest.mark.parametrize("slow", ["fallback", "primary", "neither", "failed_fallback"])
def test_each_owned_client_gets_cleanup_despite_the_other_client(monkeypatch, isolated, slow):
    from service.config.endpoints import Endpoint, Target
    target = Target("agent", Endpoint("local_provider", "http://127.0.0.1:8775", "none",
                                       provider="openai-compatible"), "synthetic")
    monkeypatch.setattr(main, "role_target", lambda role: target)
    monkeypatch.setattr(main, "cloud_super_model_enabled", lambda: False)
    owned = AsyncMock()
    monkeypatch.setattr(main, "OMLXClient", lambda **kwargs: owned)

    async def scenario():
        closed = []
        diagnostics = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: diagnostics.append(context["message"]))
        async def close(name):
            try:
                if slow == name:
                    await asyncio.Future()
                if slow == "failed_fallback" and name == "fallback":
                    raise ValueError("synthetic cleanup failure")
            finally:
                closed.append(name)
        async def fallback(self):
            await close("fallback")
        monkeypatch.setattr(main.TurnInferenceClient, "close_fallback", fallback)
        owned.aclose.side_effect = lambda: None
        async def primary():
            await close("primary")
        owned.aclose.side_effect = primary
        response = await main.agent({"prompt": "delete the folder ~/Desktop/old-stuff"})
        await serve_synthetic(response)
        assert closed == ["fallback", "primary"]
        assert isolated.runner.done() and isolated.cancelled
        assert not main.SESSIONS and not idle.foreground_busy()
        assert not isolated.approver.has_pending("call_0")
        assert bool(diagnostics) is (slow != "neither")
    asyncio.run(scenario())


@pytest.mark.parametrize("send_failure", [False, True])
def test_noncooperative_runner_deadline_is_reported_without_masking_cancellation(monkeypatch, send_failure):
    from starlette.requests import ClientDisconnect
    async def scenario():
        release = asyncio.Event()
        diagnostics = []
        runners = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: diagnostics.append(context))
        async def resistant(client, model, messages, emit, approver, **kw):
            runners.append(asyncio.current_task())
            await emit({"type": "confirm"})
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass  # deliberately violates the real runner's cooperative contract
            return "synthetic"
        monkeypatch.setattr(main, "run_agent", resistant)
        response = await main.agent({"prompt": "delete the folder ~/Desktop/old-stuff", "test_mode": True})
        confirmed = asyncio.Event()
        async def send(message):
            if message.get("body") and json.loads(message["body"][6:])["type"] == "confirm":
                confirmed.set()
                if send_failure:
                    raise OSError("synthetic closed transport")
                await asyncio.Future()
        async def receive():
            raise AssertionError("ASGI 2.4 does not receive")
        task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
        canceller = None
        try:
            await asyncio.wait_for(confirmed.wait(), 2)
            began = loop.time()
            if not send_failure:
                task.cancel()
                async def cancel_during_final_grace():
                    await asyncio.sleep(3.02)
                    while not task.done():
                        task.cancel()
                        await asyncio.sleep(0.005)
                canceller = asyncio.create_task(cancel_during_final_grace())
            with pytest.raises(ClientDisconnect if send_failure else asyncio.CancelledError):
                await asyncio.wait_for(task, 4)
            assert loop.time() - began < 3.6, "repeated cancellation must not restart the final grace"
            assert not main.SESSIONS
            assert any("suppressed cancellation" in d["message"] for d in diagnostics)
            assert not runners[0].done(), "this fixture proves the explicit cooperative boundary"
        finally:
            if canceller:
                canceller.cancel()
                await asyncio.gather(canceller, return_exceptions=True)
            release.set()
            await asyncio.wait_for(asyncio.gather(*runners), 2)
        assert runners[0].done() and not idle.foreground_busy()
    asyncio.run(scenario())
