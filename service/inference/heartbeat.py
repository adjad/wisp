"""Heartbeat an async iterator without cancelling a silent producer."""
from __future__ import annotations

import asyncio


async def with_heartbeats(events, emit, *, interval: float = 8.0):
    iterator = events.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                await emit({"type": "heartbeat"})
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield event
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if hasattr(iterator, "aclose"):
            await iterator.aclose()
