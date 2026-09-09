"""A scheduled send must never be delivered twice by a crash.

The queue used to go due() -> send -> mark(), so an interruption between the
native send and the status write left the row `pending`: the next sweep would
deliver the same message again, and the user would never be told.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from service.assistant.outbound_queue import OutboundQueue


def _queue():
    temp = tempfile.TemporaryDirectory(prefix="wisp-outbound-")
    return temp, OutboundQueue(Path(temp.name) / "outbound.db")


def _queued(queue, *, when_ts: float) -> str:
    return queue.add(channel="message", recipient="+15551234567",
                     body="I am on my way", when_ts=when_ts, subject="",
                     display="+15551234567")


def test_a_claimed_row_is_no_longer_due():
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        assert [row["id"] for row in queue.due()] == [sid]
        assert queue.claim(sid) is True
        assert queue.due() == []
    finally:
        temp.cleanup()


def test_a_row_cannot_be_claimed_twice():
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        assert queue.claim(sid) is True
        # The second sweep — or a second worker — must not deliver it again.
        assert queue.claim(sid) is False
    finally:
        temp.cleanup()


def test_an_interrupted_send_becomes_unknown_and_is_never_retried():
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        queue.claim(sid)  # crash happens here, before mark()

        recovered = queue.recover_in_flight()
        assert [row["id"] for row in recovered] == [sid]
        # Reported as an unknown outcome, not re-queued for delivery.
        assert queue.due() == []
        assert queue.recover_in_flight() == []
    finally:
        temp.cleanup()


def test_a_completed_send_is_untouched_by_recovery():
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        queue.claim(sid)
        queue.mark(sid, "sent")
        assert queue.recover_in_flight() == []
        assert queue.due() == []
    finally:
        temp.cleanup()


def _sweep(queue, hub_events):
    """Run the real scheduler sweep against a temp queue and a recording hub."""
    import asyncio
    from service.assistant import outbound_queue as queue_module
    from service.assistant import scheduler

    class _Hub:
        async def publish(self, event):
            hub_events.append(event)

    real_queue, real_hub = queue_module.outbound_queue, scheduler.hub
    queue_module.outbound_queue, scheduler.hub = queue, _Hub()
    try:
        asyncio.run(scheduler._fire_scheduled_sends())
    finally:
        queue_module.outbound_queue, scheduler.hub = real_queue, real_hub


def test_the_notice_is_republished_until_the_app_acknowledges_it():
    """Publishing is not delivery.

    The hub is in-memory: a subscriber existing proves a queue exists, not that
    the app received the notice or recorded it. So the notice carries a stable
    id, is republished every sweep, and only clears on an explicit ack.
    """
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        queue.claim(sid)  # crash here, before mark()

        # Sweep 1 — published while the app is disconnected. Nothing acks it.
        events: list[dict] = []
        _sweep(queue, events)
        unknown = [e for e in events if e["type"] == "scheduled_send_unknown"]
        assert len(unknown) == 1
        assert unknown[0]["notice_id"] == sid

        # Sweep 2 — still unacknowledged, so it comes back with the SAME id,
        # which is what lets the app drop the replay rather than the user
        # seeing it twice.
        events.clear()
        _sweep(queue, events)
        replay = [e for e in events if e["type"] == "scheduled_send_unknown"]
        assert len(replay) == 1
        assert replay[0]["notice_id"] == sid

        # The app reconnects, records it, and acknowledges.
        assert queue.acknowledge(sid) is True

        events.clear()
        _sweep(queue, events)
        assert [e for e in events if e["type"] == "scheduled_send_unknown"] == []
        # And it was never re-sent at any point.
        assert queue.due() == []
    finally:
        temp.cleanup()


def test_acknowledging_is_idempotent_and_never_resurrects_a_send():
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        queue.claim(sid)
        queue.recover_in_flight()
        assert queue.acknowledge(sid) is True
        assert queue.acknowledge(sid) is False
        assert queue.unacknowledged_unknown() == []
        assert queue.due() == []
    finally:
        temp.cleanup()
