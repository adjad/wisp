"""Persistent assistant delivery with an in-process wakeup channel.

Subscribe before taking the pending snapshot. Both replay and live delivery
carry the same immutable event id, so that boundary cannot lose or duplicate
an event within one connection. Action-request callers explicitly choose the
transient bridge for non-replayable outbound effects.
"""
from __future__ import annotations

import asyncio


class Hub:
    def __init__(self, store=None) -> None:
        self._store = store
        self._subs: set[asyncio.Queue] = set()

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
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict, *, dedupe_key: str | None = None,
                      target: dict | None = None, durable: bool = True,
                      expires_at: float | None = None) -> dict:
        # These legacy mirror commands have Void native callbacks and cannot
        # safely replay. Keep their original best-effort behavior until they
        # have a verified native receipt protocol of their own.
        if event.get('type') in {'create_apple_reminder', 'update_apple_reminder',
                                 'delete_apple_reminder'}:
            durable = False
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

    async def events(self):
        q = self.subscribe()
        seen: set[str] = set()
        try:
            for row in self.store.pending_events():
                q.put_nowait({**row['payload'], 'event_id': row['id']})
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                    continue
                event_id = event.get('event_id')
                if event_id:
                    if event_id in seen or not self.store.event_attempt(event_id):
                        continue
                    seen.add(event_id)
                yield event
        finally:
            self.unsubscribe(q)


hub = Hub()
