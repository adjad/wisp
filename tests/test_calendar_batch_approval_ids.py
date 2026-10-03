"""Calendar batch IDs isolate real pending cards; all execution is synthetic.

Eight UUID-derived hex digits avoid constant ID reuse, not all collisions.
These tests exercise loop/approver behavior, not the held HTTP request scope.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import re
import socket
import subprocess

import pytest

from service.agent import loop
from service.agent.approver import InteractiveApprover
from service.assistant import outbox
from service.inference.omlx_client import OMLXClient
from service.memory import identity
from service.safety import grants, policy
from service.tools import action_tools, assistant_tools  # register before fakes
from service.tools.registry import REGISTRY


class BatchClient:
    """One actual model-step batch followed by a final synthetic response."""

    def __init__(self, label):
        self.label = label
        self.steps = 0
        self.calls = [
            {"id": "cancel", "function": {"name": "cancel_event", "arguments":
                json.dumps({"title": f"{label} old"})}},
            {"id": "create", "function": {"name": "add_calendar_event", "arguments":
                json.dumps({"title": f"{label} new", "when_iso": "2036-09-28T18:00:00-07:00",
                            "duration_min": 60, "location": "fixture room"})}},
        ]

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        self.steps += 1
        assert self.steps <= 2, "no model retry after final settlement"
        offered = {schema["function"]["name"] for schema in tools or []}
        assert {"cancel_event", "add_calendar_event"} <= offered
        yield {"kind": "final", "message": {
            "role": "assistant", "content": "" if self.steps == 1 else "Fixture complete.",
            "tool_calls": deepcopy(self.calls) if self.steps == 1 else None}}


@pytest.fixture
def inert_calendar(monkeypatch, tmp_path):
    calls = []
    attempts = dict.fromkeys(("network", "process", "model", "bridge"), 0)
    failures = {}

    def forbidden(kind):
        def reject(*args, **kwargs):
            attempts[kind] += 1
            raise AssertionError(f"real {kind} path attempted")
        return reject

    monkeypatch.setattr(socket.socket, "connect", forbidden("network"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("process"))
    monkeypatch.setattr(OMLXClient, "ensure_only", forbidden("model"))
    monkeypatch.setattr(OMLXClient, "stream_events", forbidden("model"))
    monkeypatch.setattr(outbox, "request", forbidden("bridge"))
    monkeypatch.setattr(identity, "_full_name", "Fixture User")
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    monkeypatch.setattr(loop, "audit", lambda *args, **kwargs: None)

    for name in ("cancel_event", "add_calendar_event"):
        def fake(*, _name=name, **args):
            calls.append((_name, deepcopy(args)))
            return failures.get(_name, f"Fixture {_name} succeeded: {args['title']}")
        monkeypatch.setitem(REGISTRY, name, replace(REGISTRY[name], func=fake))

    yield calls, failures
    assert attempts == {key: 0 for key in attempts}
    print("BATCH_EFFECT_ATTEMPTS", json.dumps(attempts, sort_keys=True))


async def _run(client, approver, events):
    async def emit(event):
        events.append(deepcopy(event))
    return await loop.run_agent(
        client, "fixture-model", [{"role": "user", "content": "Make the two calendar changes."}],
        emit, approver, tools=["cancel_event", "add_calendar_event"],
        max_steps=3, include_memory_context=False, debug=False)


def _assert_card(card, label, approver):
    assert card["type"] == "confirm" and card["tool"] == "calendar_changes"
    assert card["args"] == {}  # existing batch contract is a preview, not HTTP scope
    assert card["reason"] == "changes your calendar in 2 ways — always confirmed"
    assert card["preview"].splitlines()[0] == f"Cancel: {label} old"
    assert f"{label} new" in card["preview"]
    assert assistant_tools.calendar_interval_label("2036-09-28T18:00:00-07:00", 60) in card["preview"]
    assert card["id"] in approver._pending and card["id"] in approver._actions


def _assert_settled(calls, label, approved, events, client, text):
    selected = [(name, args) for name, args in calls if args["title"].startswith(label + " ")]
    if approved:
        assert [name for name, args in selected] == ["cancel_event", "add_calendar_event"]
        assert selected[0][1] == {"title": f"{label} old"}
        assert selected[1][1] == {"title": f"{label} new", "when_iso": "2036-09-28T18:00:00-07:00",
                                   "duration_min": 60, "location": "fixture room"}
        assert client.steps == 2
        assert "denied" not in text.lower()
    else:
        assert selected == [] and client.steps == 1
        assert "approval was denied" in text
        assert any(event.get("result") == "The user denied this action." for event in events)


async def _finish(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.asyncio
async def test_sequential_production_batches_emit_distinct_ids(inert_calendar, approved):
    calls, _ = inert_calendar
    queue = asyncio.Queue()
    approver = InteractiveApprover(queue.put, timeout=2)
    ids = []
    for label in ("first", "second"):
        events, client = [], BatchClient(label)
        task = asyncio.create_task(_run(client, approver, events))
        try:
            card = await asyncio.wait_for(queue.get(), 1)
            _assert_card(card, label, approver)
            assert re.fullmatch(r"batch_calendar_[0-9a-f]{8}", card["id"])
            assert card["id"] not in ids
            ids.append(card["id"])
            assert approver.resolve(card["id"], approved)
            text = await asyncio.wait_for(task, 1)
            _assert_settled(calls, label, approved, events, client, text)
            assert not approver.resolve(card["id"], not approved)
            assert approver._pending == {} and approver._actions == {}
        finally:
            await _finish([task])


@pytest.mark.parametrize("verdicts", [(True, False), (False, True), (True, True), (False, False)])
@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
@pytest.mark.asyncio
async def test_overlapping_batches_do_not_replace_or_resolve_each_other(
        inert_calendar, verdicts, order):
    calls, _ = inert_calendar
    queue = asyncio.Queue()
    approver = InteractiveApprover(queue.put, timeout=2)
    clients = [BatchClient("left"), BatchClient("right")]
    events = [[], []]
    tasks = []
    cards = []
    try:
        for index, label in enumerate(("left", "right")):
            tasks.append(asyncio.create_task(_run(clients[index], approver, events[index])))
            card = await asyncio.wait_for(queue.get(), 1)
            _assert_card(card, label, approver)
            cards.append(card)
        assert cards[0]["id"] != cards[1]["id"]
        assert set(approver._pending) == {card["id"] for card in cards}
        assert set(approver._actions) == set(approver._pending)
        first, second = order
        untouched = approver._pending[cards[second]["id"]]
        assert calls == [] and all(not task.done() for task in tasks)
        assert not approver.resolve("batch_calendar", True)
        assert not approver.resolve("unknown-card", True)
        assert approver.resolve(cards[first]["id"], verdicts[first])
        assert not approver.resolve(cards[first]["id"], not verdicts[first])
        text = await asyncio.wait_for(tasks[first], 1)
        _assert_settled(calls, clients[first].label, verdicts[first], events[first], clients[first], text)
        assert not untouched.done() and not tasks[second].done()
        assert set(approver._pending) == {cards[second]["id"]}
        assert set(approver._actions) == set(approver._pending)
        assert approver.resolve(cards[second]["id"], verdicts[second])
        text = await asyncio.wait_for(tasks[second], 1)
        _assert_settled(calls, clients[second].label, verdicts[second], events[second], clients[second], text)
        assert approver._pending == {} and approver._actions == {}
        assert all(not approver.resolve(card["id"], True) for card in cards)
    finally:
        await _finish(tasks)


@pytest.mark.asyncio
async def test_request_local_approvers_reject_the_other_live_batch_id(inert_calendar):
    calls, _ = inert_calendar
    queues = [asyncio.Queue(), asyncio.Queue()]
    approvers = [InteractiveApprover(queue.put, timeout=2) for queue in queues]
    clients = [BatchClient("left"), BatchClient("right")]
    events = [[], []]
    tasks = [asyncio.create_task(_run(client, approver, event))
             for client, approver, event in zip(clients, approvers, events)]
    try:
        cards = [await asyncio.wait_for(queue.get(), 1) for queue in queues]
        for index in range(2):
            _assert_card(cards[index], clients[index].label, approvers[index])
            assert not approvers[index].resolve(cards[1 - index]["id"], True)
            assert not tasks[index].done()
        assert calls == []
        for approver, card in zip(approvers, cards):
            assert approver.resolve(card["id"], False)
        texts = await asyncio.wait_for(asyncio.gather(*tasks), 1)
        for index in range(2):
            _assert_settled(calls, clients[index].label, False, events[index], clients[index], texts[index])
            assert approvers[index]._pending == {} and approvers[index]._actions == {}
    finally:
        await _finish(tasks)


@pytest.mark.asyncio
async def test_approved_batch_stops_after_uncertain_first_result_without_retry(inert_calendar):
    calls, failures = inert_calendar
    failures["cancel_event"] = "(error: outcome unknown. Check Calendar; do not retry automatically.)"
    queue = asyncio.Queue()
    approver = InteractiveApprover(queue.put, timeout=2)
    client, events = BatchClient("uncertain"), []
    task = asyncio.create_task(_run(client, approver, events))
    try:
        card = await asyncio.wait_for(queue.get(), 1)
        _assert_card(card, "uncertain", approver)
        assert approver.resolve(card["id"], True)
        text = await asyncio.wait_for(task, 1)
        assert calls == [("cancel_event", {"title": "uncertain old"})]
        assert client.steps == 1
        assert "I stopped without retrying" in text
        assert "Check Calendar; do not retry automatically" in text
        assert approver._pending == {} and approver._actions == {}
        assert not approver.resolve(card["id"], True)
    finally:
        await _finish([task])
