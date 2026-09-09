"""Tiny in-process pub/sub so the scheduler can push assistant events to any
connected /assistant/events SSE clients (reminders firing, the commitment set
changing so the notch chip re-fetches)."""
from __future__ import annotations

import asyncio


class Hub:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @property
    def live(self) -> bool:
        """Whether anything is actually connected to receive an event."""
        return bool(self._subs)

    async def publish(self, event: dict) -> None:
        for q in list(self._subs):
            await q.put(event)


hub = Hub()
