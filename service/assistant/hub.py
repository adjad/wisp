"""Persistent assistant delivery with an in-process wakeup channel.

Subscribe before taking the pending snapshot. Both replay and live delivery
carry the same immutable event id, so that boundary cannot lose or duplicate
an event within one connection. Action-request callers explicitly choose the
transient bridge for non-replayable outbound effects.
"""
from __future__ import annotations

import asyncio
import time
import threading
import json

from service import evaluation_boundary

# The server ends each app event stream on a timer (events(rotate_after=...))
# and the app re-subscribes 3s later (OverlayModel.startAssistant). A
# subscriber gone for less than this is treated as reconnecting, not absent.
RECONNECT_WINDOW_S = 60.0


class Hub:
    def __init__(self, store=None) -> None:
        self._store = store
        self._subs: set[asyncio.Queue] = set()
        self._waiters: set[asyncio.Future] = set()
        self._last_unsubscribed: float | None = None
        self._owner_loop = None
        self._owner_thread = None

    @property
    def store(self):
        if self._store is not None:
            return self._store
        from service.assistant.store import assistant_store
        return assistant_store

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subs)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(True)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.discard(q)
            if not self._subs:
                self._last_unsubscribed = time.monotonic()

    async def wait_for_subscriber(self, timeout: float) -> bool:
        """True once the app is subscribed, waiting only across a reconnect.

        Without a subscriber that left within RECONNECT_WINDOW_S, the app is
        not running (or never connected) and this returns False at once.
        """
        if self._subs:
            return True
        if (self._last_unsubscribed is None
                or time.monotonic() - self._last_unsubscribed > RECONNECT_WINDOW_S):
            return False
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._waiters.discard(waiter)
        return bool(self._subs)

    async def publish(self, event: dict, *, dedupe_key: str | None = None,
                      target: dict | None = None, durable: bool = True,
                      expires_at: float | None = None) -> dict:
        return self.publish_sync(event,dedupe_key=dedupe_key,target=target,
                                 durable=durable,expires_at=expires_at)

    def publish_sync(self, event: dict, *, dedupe_key: str | None = None,
                      target: dict | None = None, durable: bool = True,
                      expires_at: float | None = None) -> dict:
        # The contained evaluation host publishes nothing: inert pings are
        # dropped, anything else raises before the durable queue or fanout.
        if not evaluation_boundary.publication_allowed(event, "publish_sync"):
            return event
        if evaluation_boundary.active():
            # The one admitted evaluation event is never queued or replayed.
            durable = False
        # The legacy Void bridge cannot return verified results. Refuse its
        # commands so callers cannot bypass the claimed Reminders protocol.
        if event.get('type') in {'create_apple_reminder', 'update_apple_reminder',
                                 'delete_apple_reminder'}:
            raise ValueError('legacy Reminders effect has no verified receipt')
        if durable:
            row = (self.store.event(event['event_id']) if event.get('event_id') else
                   self.store.enqueue_event(event, dedupe_key=dedupe_key,
                                            target=target, expires_at=expires_at))
            if row is None:
                raise ValueError('unknown event id')
            event = {**row['payload'], 'event_id': row['id']}
            if row['state'] != 'pending':
                return event
        for q in list(self._subs):
            q.put_nowait(event)
        return event

    def prepare_nudge_delivery(self, store):
        """Bind the actual store and loop before any publication lease."""
        evaluation_boundary.publication_allowed({"type": "nudge_delivery"}, "prepare_nudge_delivery")
        from service.assistant.store import AssistantStore, CommittedNudge
        if type(self) is not Hub or type(store) is not AssistantStore or self.store is not store:
            raise ValueError("Nudge delivery needs the actual matching Hub and Store")
        loop = asyncio.get_running_loop()
        if self._owner_loop is not None and self._owner_loop is not loop:
            if not self._owner_loop.is_closed() or self._subs or self._waiters:
                raise ValueError("Hub belongs to another event loop")
        if any(type(q) is not asyncio.Queue or q._loop not in (None,loop) for q in self._subs):
            raise ValueError("Hub subscriber belongs to another loop")
        self._store,self._receipt_type = store,CommittedNudge
        self._owner_loop,self._owner_thread = loop,threading.get_ident()
        return loop

    def publish_committed_sync(self, lease, receipt, runtime, loop):
        """Fan out only a fixed writer's committed canonical row, with no await."""
        evaluation_boundary.publication_allowed({"type": "committed_nudge"}, "publish_committed_sync")
        if (self._owner_loop is not loop or asyncio.get_running_loop() is not loop
                or self._owner_thread!=threading.get_ident() or type(receipt) is not self._receipt_type):
            raise ValueError("Exact prepared delivery loop and receipt required")
        self._store.validate_committed_nudge(lease,receipt,runtime)
        event = {**json.loads(receipt.payload),"event_id":receipt.event_id}
        if receipt.pending:
            for q in list(self._subs):
                q.put_nowait(event)
            if event.get("auto_performed"):
                for q in list(self._subs):
                    q.put_nowait({"type":"changed"})
        return event

    async def events(self, rotate_after: float | None = None):
        """Replay pending events, then stream live ones.

        With `rotate_after`, the SERVER ends the stream after that many
        seconds so failed deliveries are retried on a fresh replay. The server
        must be the side that rotates: when the app cut the stream itself it
        stopped reading while this queue was still subscribed, and a transient
        outbound send published in that window was silently lost. Rotation
        unsubscribes first (new publishers then see no subscriber and wait for
        the reconnect) and hands over whatever was already queued.
        """
        # No subscriber may drain replayed actions inside the evaluation host.
        evaluation_boundary.publication_allowed({"type": "events_subscription"}, "events")
        q = self.subscribe()
        seen: set[str] = set()
        deadline = None if rotate_after is None else time.monotonic() + rotate_after

        def deliverable(event: dict) -> bool:
            event_id = event.get('event_id')
            if event_id:
                if event_id in seen or not self.store.event_attempt(event_id):
                    return False
                seen.add(event_id)
            return True

        evaluating = evaluation_boundary.active()
        try:
            # An evaluation stream replays nothing: no queued row is ever drained.
            for row in ([] if evaluating else self.store.pending_events()):
                q.put_nowait({**row['payload'], 'event_id': row['id']})
            while True:
                wait = 15.0
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    wait = min(wait, remaining)
                try:
                    event = await asyncio.wait_for(q.get(), timeout=wait)
                except asyncio.TimeoutError:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    yield {"type": "heartbeat"}
                    continue
                if evaluating and not evaluation_boundary.subscriber_may_receive(event):
                    continue
                if deliverable(event):
                    yield event
            self.unsubscribe(q)
            while not q.empty():
                event = q.get_nowait()
                if evaluating and not evaluation_boundary.subscriber_may_receive(event):
                    continue
                if deliverable(event):
                    yield event
        finally:
            self.unsubscribe(q)

hub = Hub()
