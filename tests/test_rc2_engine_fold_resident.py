"""The rolling-summary fold after a turn is resident-only: it never loads or evicts.

It called ``client.ensure_only(fast model)``. On Wisp's engine that unloads every
resident model outside the keep-warm set, then loads the fast model. With a turn
model that isn't the summary model (a multi-model roster), the fold after each
turn swapped the turn's model out. The fold now runs only on a summary model the
engine already holds, and otherwise stands down.

Fake engine client only: no engine, no model, no socket.
"""
import asyncio
import socket
import subprocess
from unittest.mock import AsyncMock

import pytest

from service import main
from service.memory import context

TURN_MODEL = "Big-Agent-27B-oQ4"
OTHER_APP_MODEL = "Someone-Elses-Model-8B"


@pytest.fixture
def engine(monkeypatch):
    """A fake engine client with two models loaded: the turn's model and the summary model."""
    fast = main.role_target("fast").model
    assert fast != TURN_MODEL
    client = AsyncMock()
    client.loaded = [TURN_MODEL, fast]
    client.chats = []

    async def loaded_models():
        return list(client.loaded)

    async def chat(model, messages, **kwargs):
        client.chats.append(model)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Earlier: notes."}}]}

    async def ensure_only(model, **kwargs):
        # What the bundled engine path does: evict everything else, then load.
        client.loaded = [model]

    client.loaded_models.side_effect = loaded_models
    client.chat.side_effect = chat
    client.ensure_only.side_effect = ensure_only
    client.fast = fast

    main.SESSIONS.clear()
    monkeypatch.setattr(main, "client", client, raising=False)
    monkeypatch.setattr(main, "ensure_omlx", AsyncMock())
    monkeypatch.setattr(main, "maybe_summarize", context.maybe_summarize)
    monkeypatch.setattr(main, "_SUMMARY_TASKS", {})
    monkeypatch.setattr(context, "_fold_failures", {})
    monkeypatch.setattr(context, "_fallback_logged", set())

    async def quick(client_, model, messages, emit, approver, **kw):
        await emit({"type": "text", "text": "all done"})
        await emit({"type": "done"})
        return "all done"
    monkeypatch.setattr(main, "run_agent", quick, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("fold fixtures must not open sockets or native processes")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    yield client
    main.SESSIONS.clear()


def _long_chat():
    sid = main.store.create_session()
    for index in range(context.KEEP_MESSAGES + context._FOLD_BATCH):
        main.store.add_turn(sid, "user" if index % 2 == 0 else "assistant", f"earlier {index}")
    return sid


async def _turn_then_fold(client, sid):
    response = await main.agent({"prompt": "tell me something", "session_id": sid})
    async for _ in response.body_iterator:
        pass
    # Only what the background fold does from here on.
    for name in ("ensure_only", "load", "unload"):
        getattr(client, name).reset_mock()
    fold = main._SUMMARY_TASKS.get(sid)
    assert fold is not None, "the turn started a fold"
    await asyncio.wait_for(fold, 5)


def _assert_no_residency_change(client, loaded_before):
    client.ensure_only.assert_not_awaited()
    client.load.assert_not_awaited()
    client.unload.assert_not_awaited()
    assert client.loaded == loaded_before


def test_fold_with_two_models_loaded_uses_the_resident_one_and_evicts_nothing(engine):
    sid = _long_chat()

    async def scenario():
        await _turn_then_fold(engine, sid)
    asyncio.run(scenario())
    _assert_no_residency_change(engine, [TURN_MODEL, engine.fast])
    assert engine.chats == [engine.fast]
    session = main.store.get_session(sid)
    assert session["summary"] == "Earlier: notes."
    assert session["summarized_idx"] > 0


def test_fold_stands_down_when_its_model_isnt_resident(engine):
    # The user's own oMLX, holding the turn's model and another app's model.
    engine.loaded = [TURN_MODEL, OTHER_APP_MODEL]
    sid = _long_chat()

    async def scenario():
        await _turn_then_fold(engine, sid)
    asyncio.run(scenario())
    _assert_no_residency_change(engine, [TURN_MODEL, OTHER_APP_MODEL])
    assert engine.chats == []
    assert context._fold_failures[sid] == 1
    assert main.store.get_session(sid)["summarized_idx"] == 0


def test_a_summary_model_never_resident_still_gets_the_condensed_fallback(engine):
    engine.loaded = [TURN_MODEL]
    sid = _long_chat()

    async def prepare():
        await context.require_resident(engine, engine.fast)

    async def scenario():
        for _ in range(context._MAX_FOLD_FAILURES):
            await context.maybe_summarize(engine, sid, engine.fast, prepare=prepare)
    asyncio.run(scenario())
    _assert_no_residency_change(engine, [TURN_MODEL])
    assert engine.chats == []
    session = main.store.get_session(sid)
    assert session["summarized_idx"] == context._FOLD_BATCH
    assert session["summary"].startswith(context._FALLBACK_HEAD)


def test_require_resident_reads_status_only(engine):
    async def scenario():
        await context.require_resident(engine, engine.fast)
        with pytest.raises(context.FoldModelNotResident):
            await context.require_resident(engine, "Not-Loaded-Model")
    asyncio.run(scenario())
    _assert_no_residency_change(engine, [TURN_MODEL, engine.fast])
    assert engine.chats == []
