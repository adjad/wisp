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
        async def publish(self, event, **metadata):
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


class _App:
    """Stands in for the app's event subscription and its native send."""

    def __init__(self, connected: bool):
        self.has_subscribers = connected
        self.sent: list[dict] = []
        self.drop_on_send = False

    async def request(self, operation, payload):
        if self.drop_on_send:
            # The subscription went away between the scheduler's check and the
            # send; the host proves nothing was dispatched.
            self.has_subscribers = False
        if not self.has_subscribers:
            return {"ok": False, "status": "not_attempted", "dispatch_started": False,
                    "error": "Wisp app is not connected; nothing was sent"}
        self.sent.append({"type": operation, **payload})
        return {"ok": True, "status": "succeeded", "dispatch_started": True}


def _tick(monkeypatch, queue, app, hub_events):
    """One real scheduler sweep, with the app's connection under test control."""
    from service.assistant import outbox
    monkeypatch.setattr(outbox, "hub", app)
    monkeypatch.setattr(outbox, "request", app.request)
    _sweep(queue, hub_events)


def test_a_send_due_before_the_app_connects_waits_and_then_goes_once(monkeypatch):
    """Wisp was closed at send time and opened inside the late window.

    The first scheduler tick runs before the app has subscribed. The send must
    stay pending, with no failure notice, and go out once the app is there.
    """
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5 * 60)
        app, events = _App(connected=False), []

        _tick(monkeypatch, queue, app, events)
        assert queue.get(sid)["status"] == "pending"
        assert queue.get(sid)["fired_at"] is None
        assert app.sent == [] and events == []

        app.has_subscribers = True
        _tick(monkeypatch, queue, app, events)
        assert queue.get(sid)["status"] == "sent"
        assert [e["text"] for e in app.sent] == ["I am on my way"]
        assert [(e["type"], e["ok"]) for e in events] == [("scheduled_send_result", True)]

        # Later ticks never send it again.
        events.clear()
        _tick(monkeypatch, queue, app, events)
        assert len(app.sent) == 1 and events == []
    finally:
        temp.cleanup()


def test_a_send_more_than_thirty_minutes_late_is_missed_not_sent(monkeypatch):
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 31 * 60)
        for connected in (False, True):
            app, events = _App(connected=connected), []
            _tick(monkeypatch, queue, app, events)
            assert queue.get(sid)["status"] == "missed"
            assert app.sent == []
            # Told once, on the tick that retired it.
            assert [e["type"] for e in events] == (
                [] if connected else ["scheduled_send_missed"])
    finally:
        temp.cleanup()


def test_a_send_that_waited_for_the_app_past_the_window_is_missed(monkeypatch):
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 29 * 60)
        app, events = _App(connected=False), []
        _tick(monkeypatch, queue, app, events)
        assert queue.get(sid)["status"] == "pending" and events == []

        # Still nobody two minutes on: now it is too late to send.
        queue._db.execute("UPDATE scheduled_sends SET when_ts = when_ts - 120")
        queue._db.commit()
        app.has_subscribers = True
        _tick(monkeypatch, queue, app, events)
        assert queue.get(sid)["status"] == "missed" and app.sent == []
        assert [e["type"] for e in events] == ["scheduled_send_missed"]
    finally:
        temp.cleanup()


def test_the_app_dropping_at_send_time_puts_the_send_back_not_failed(monkeypatch):
    temp, queue = _queue()
    try:
        first = _queued(queue, when_ts=time.time() - 60)
        second = _queued(queue, when_ts=time.time() - 30)
        app, events = _App(connected=True), []
        app.drop_on_send = True

        _tick(monkeypatch, queue, app, events)
        # Neither was dispatched, neither is failed, and nobody was told.
        assert queue.get(first)["status"] == "pending"
        assert queue.get(second)["status"] == "pending"
        assert app.sent == [] and events == []

        app.drop_on_send, app.has_subscribers = False, True
        _tick(monkeypatch, queue, app, events)
        assert queue.get(first)["status"] == "sent"
        assert queue.get(second)["status"] == "sent"
        assert len(app.sent) == 2
    finally:
        temp.cleanup()


def test_a_send_refused_before_dispatch_while_connected_still_fails(monkeypatch):
    """Putting a row back is only for a missing app, not for every refusal."""
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        app, events = _App(connected=True), []

        async def refused(operation, payload):
            return {"ok": False, "status": "not_attempted", "dispatch_started": False,
                    "error": "Invalid outbound action payload; nothing was sent"}
        app.request = refused

        _tick(monkeypatch, queue, app, events)
        row = queue.get(sid)
        assert row["status"] == "failed"
        assert row["error"] == "Invalid outbound action payload; nothing was sent"
        assert [(e["type"], e["ok"]) for e in events] == [("scheduled_send_result", False)]
        assert queue.due() == []
    finally:
        temp.cleanup()


def test_release_only_returns_a_claimed_row():
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        assert queue.release(sid) is False          # pending: nothing to release
        assert queue.claim(sid) is True
        assert queue.release(sid) is True
        assert [row["id"] for row in queue.due()] == [sid]

        # Anything that may have gone out, or that the user stopped, stays put.
        for status in ("sent", "unknown", "failed", "missed", "cancelled"):
            queue.claim(sid)
            queue.mark(sid, status)
            assert queue.release(sid) is False
            assert queue.get(sid)["status"] == status
            assert queue.due() == []
            queue._db.execute("UPDATE scheduled_sends SET status='pending' WHERE id=?", (sid,))
            queue._db.commit()
    finally:
        temp.cleanup()


def test_view_only_at_send_time_stops_a_scheduled_send(monkeypatch):
    """Approved on a card earlier, but the user has since switched to View-only."""
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        app, events = _App(connected=True), []
        monkeypatch.setattr("service.safety.read_only", lambda: True)

        _tick(monkeypatch, queue, app, events)
        row = queue.get(sid)
        assert app.sent == []
        assert row["status"] == "failed"
        assert row["error"] == "View-only mode was on; nothing was sent"
        assert events == [{"type": "scheduled_send_result", "ok": False,
                           "channel": "message", "display": "+15551234567",
                           "error": "View-only mode was on; nothing was sent"}]

        # Switching back does not revive it: nothing goes out later, unasked.
        monkeypatch.setattr("service.safety.read_only", lambda: False)
        events.clear()
        _tick(monkeypatch, queue, app, events)
        assert app.sent == [] and events == []
        assert queue.get(sid)["status"] == "failed"
    finally:
        temp.cleanup()


def test_a_scheduled_send_goes_out_when_view_only_is_off(monkeypatch):
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        app, events = _App(connected=True), []
        monkeypatch.setattr("service.safety.read_only", lambda: False)

        _tick(monkeypatch, queue, app, events)
        assert [e["type"] for e in app.sent] == ["send_message"]
        assert queue.get(sid)["status"] == "sent"
        assert [(e["type"], e["ok"]) for e in events] == [("scheduled_send_result", True)]
    finally:
        temp.cleanup()


def test_view_only_does_not_undo_a_cancel(monkeypatch):
    temp, queue = _queue()
    try:
        sid = _queued(queue, when_ts=time.time() - 5)
        assert queue.cancel(sid) is not None
        app, events = _App(connected=True), []
        monkeypatch.setattr("service.safety.read_only", lambda: True)

        _tick(monkeypatch, queue, app, events)
        assert queue.get(sid)["status"] == "cancelled"
        assert app.sent == [] and events == []
    finally:
        temp.cleanup()
