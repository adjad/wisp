"""The app's routine event-stream reconnect must not fail native actions, and a
turn that stops on a failed or denied write must still end with `done`.

Regression for debug export 2026-10-12: an approved add_calendar_event landed
in the app's 3s re-subscribe gap and failed as "Wisp app is not connected";
the loop then returned without `done`, so the app replaced the honest failure
text with "The assistant didn't respond."
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
import socket
import subprocess
import time

import pytest

from service.agent import loop
from service.assistant import outbox
import service.assistant.hub  # noqa: E402
import sys
from service.assistant.hub import Hub
from service.inference.omlx_client import OMLXClient
from service.memory import identity
from service.safety import grants, policy
from service.tools import assistant_tools  # noqa: F401  register before fakes
from service.tools.registry import REGISTRY


# --- reconnect grace -------------------------------------------------------

@pytest.mark.asyncio
async def test_never_connected_fails_immediately():
    hub = Hub(store=object())
    started = time.monotonic()
    assert await outbox.app_connected(grace=5, via=hub) is False
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_resubscribe_inside_grace_counts_as_connected():
    hub = Hub(store=object())
    hub.unsubscribe(hub.subscribe())  # the app's periodic stream rotation

    async def reconnect():
        await asyncio.sleep(0.1)
        hub.subscribe()

    task = asyncio.create_task(reconnect())
    assert await outbox.app_connected(grace=2, via=hub) is True
    await task


@pytest.mark.asyncio
async def test_no_resubscribe_within_grace_still_fails():
    hub = Hub(store=object())
    hub.unsubscribe(hub.subscribe())
    started = time.monotonic()
    assert await outbox.app_connected(grace=0.2, via=hub) is False
    assert time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_long_gone_app_is_not_waited_for(monkeypatch):
    hub = Hub(store=object())
    hub.unsubscribe(hub.subscribe())
    monkeypatch.setattr(sys.modules["service.assistant.hub"], "RECONNECT_WINDOW_S", 0.0)
    await asyncio.sleep(0.01)
    started = time.monotonic()
    assert await outbox.app_connected(grace=5, via=hub) is False
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_calendar_request_waits_out_the_gap(monkeypatch):
    hub = Hub(store=object())
    hub.unsubscribe(hub.subscribe())
    published = []

    async def publish(event, **metadata):
        published.append(event)
        outbox.complete(event["action_id"], {"ok": True})
        return event

    monkeypatch.setattr(hub, "publish", publish)
    monkeypatch.setattr(outbox, "hub", hub)
    monkeypatch.setattr(outbox, "RECONNECT_GRACE_S", 2.0)

    async def reconnect():
        await asyncio.sleep(0.1)
        hub.subscribe()

    task = asyncio.create_task(reconnect())
    result = await outbox._request("create_calendar_event", {"title": "Fixture"}, timeout=2)
    await task
    assert result == {"ok": True}
    assert [event["type"] for event in published] == ["create_calendar_event"]


# --- terminal `done` on early write exits -------------------------------------

WHEN_ISO = datetime(2036, 9, 28, 12, 0).astimezone().isoformat(timespec="seconds")


class OneCallClient:
    def __init__(self):
        self.steps = 0

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        self.steps += 1
        assert self.steps == 1, "a failed write must not go back to the model"
        yield {"kind": "final", "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "create", "function": {
                "name": "add_calendar_event",
                "arguments": json.dumps({"title": "Fixture", "when_iso": WHEN_ISO,
                                         "duration_min": 60})}}]}}


class FixedApprover:
    def __init__(self, verdict):
        self.verdict = verdict

    async def confirm(self, action):
        return self.verdict


@pytest.fixture
def inert(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("real side effect attempted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(OMLXClient, "stream_events", forbidden)
    monkeypatch.setattr(outbox, "request", forbidden)
    monkeypatch.setattr(identity, "_full_name", "Fixture User")
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    monkeypatch.setattr(loop, "audit", lambda *args, **kwargs: None)

    def fake(**args):
        return "(error: Wisp app is not connected; Calendar was not changed.)"

    monkeypatch.setitem(REGISTRY, "add_calendar_event",
                        replace(REGISTRY["add_calendar_event"], func=fake))


@pytest.mark.parametrize("approved, expected", [
    (True, "did not return a verified success"),
    (False, "approval was denied"),
])
@pytest.mark.asyncio
async def test_stopped_write_turn_ends_with_done(inert, approved, expected):
    events = []

    async def emit(event):
        events.append(deepcopy(event))

    text = await loop.run_agent(
        OneCallClient(), "fixture-model",
        [{"role": "user", "content": "Add Fixture to my calendar Thursday at noon."}],
        emit, FixedApprover(approved), tools=["add_calendar_event"],
        max_steps=3, include_memory_context=False, debug=False)
    assert expected in text
    types = [event["type"] for event in events]
    assert types.count("done") == 1 and types[-1] == "done"
    assert [e for e in events if e["type"] == "text"][-1]["text"] == text
