"""Persistent assistant delivery with an in-process wakeup channel.

Subscribe before taking the pending snapshot. Both replay and live delivery
carry the same immutable event id, so that boundary cannot lose or duplicate
an event within one connection. Action-request callers explicitly choose the
transient bridge for non-replayable outbound effects.
"""
from __future__ import annotations

import asyncio
import time

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

        try:
            for row in self.store.pending_events():
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
                if deliverable(event):
                    yield event
            self.unsubscribe(q)
            while not q.empty():
                event = q.get_nowait()
                if deliverable(event):
                    yield event
        finally:
            self.unsubscribe(q)

hub = Hub()
