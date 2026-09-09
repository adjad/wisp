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
