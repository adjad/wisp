"""The live-source evaluation boundary: one transient re-read request, nothing else.

This module latches the process-wide boundary as an approved live-source run
(its own process under the simulation runner; the latch is irreversible). A
SYNTHETIC store stands in for the assistant store and fails on any use, so a
queued, replayed or acknowledged event would fail the test. Nothing here starts
the app, a model or a reader.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from service import evaluation_boundary as boundary
from service.assistant.hub import Hub


class RefusingStore:
    """Any durable queue, replay or claim access is a failure."""

    def __getattr__(self, name):
        raise AssertionError(f"the evaluation hub touched the store: {name}")


@pytest.fixture(scope="module", autouse=True)
def latched(tmp_path_factory):
    owned = os.path.realpath(tmp_path_factory.mktemp("owned"))
    boundary.latch(boundary.Context(run_id="live-boundary", owned_root=owned, source_roots=(),
                                    reads=(), live_source_sync=True))
    yield


SYNC = {"type": "sync_assistant_sources_now", "sources": ["email", "notes"]}


def test_only_the_exact_re_read_request_reaches_a_subscriber_transiently():
    hub = Hub(store=RefusingStore())
    queue = hub.subscribe()
    try:
        assert hub.publish_sync(dict(SYNC)) == SYNC            # durable by default: forced transient
        assert queue.get_nowait() == SYNC and queue.empty()
        assert asyncio.run(hub.publish(dict(SYNC), durable=True)) == SYNC
        assert queue.get_nowait() == SYNC
        refused = [
            {"type": "sync_emails_now"},                       # also fetches bodies: not admitted
            {"type": "send_email", "to": "a@example.com"},
            {"type": "send_message", "to": "x", "text": "y"},
            {"type": "create_calendar_event"}, {"type": "create_reminder"},
            {"type": "behavior_nudge"}, {"type": "attention_added", "title": "t"},
            {"type": "sync_assistant_sources_now", "sources": ["browser_history"]},
            {"type": "sync_assistant_sources_now", "sources": []},
            {"type": "sync_assistant_sources_now", "sources": ["email", "email"]},
            {"type": "sync_assistant_sources_now", "sources": "email"},
            {"type": "sync_assistant_sources_now", "sources": ["email"], "action_id": "a"},
            {"type": "sync_assistant_sources_now"},
        ]
        for event in refused:
            with pytest.raises(boundary.EvaluationBlockedError):
                hub.publish_sync(event)
        assert hub.publish_sync({"type": "changed"}) == {"type": "changed"}   # suppressed, not delivered
        assert queue.empty()
    finally:
        hub.unsubscribe(queue)


def test_nudge_and_committed_publication_stay_refused():
    hub = Hub(store=RefusingStore())
    with pytest.raises(boundary.EvaluationBlockedError):
        hub.publish_committed_sync(None, None, None, None)

    async def prepare():
        hub.prepare_nudge_delivery(RefusingStore())
    with pytest.raises(boundary.EvaluationBlockedError):
        asyncio.run(prepare())


def test_the_event_stream_replays_nothing_and_carries_only_the_request():
    hub = Hub(store=RefusingStore())

    async def run():
        stream = hub.events(rotate_after=0.5)
        first = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.05)                              # subscribed; no replay was read
        (queue,) = hub._subs
        queue.put_nowait({"type": "send_email", "to": "a@example.com"})   # never a publisher's
        queue.put_nowait({"type": "changed"})
        queue.put_nowait(dict(SYNC))
        got = [await first]
        async for event in stream:
            got.append(event)
        return got

    assert asyncio.run(run()) == [SYNC]


def test_effects_and_unreviewed_reads_stay_blocked():
    blocked = boundary.native_request_blocked("send_email", {"to": "a@example.com"})
    assert blocked["status"] == "blocked" and not blocked["dispatch_started"]
    assert "EVALUATION_BLOCKED" in boundary.tool_dispatch_problem(type("T", (), {"name": "send_email"})(), {})
    assert boundary.model_control_blocked("unload") is not None
    outcomes = {(r["sink"], r["outcome"]) for r in boundary.receipts()}
    assert ("hub", "allowed") in outcomes and ("hub", "blocked") in outcomes
    assert boundary.evidence_failure() is None
