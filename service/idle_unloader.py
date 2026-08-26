"""Background loop: unload any resident model idle past the configured
timeout (service.idle.get_idle_minutes()). Purely a memory-reclaim measure —
an unloaded model just reloads, at the usual cold-start cost, the next time
it's actually needed.
"""
from __future__ import annotations

import asyncio
import time

from service import idle
from service.inference.omlx_client import OMLXClient


async def run(client: OMLXClient, poll_seconds: float = 30.0) -> None:
    while True:
        await asyncio.sleep(poll_seconds)
        minutes = idle.get_idle_minutes()
        if minutes <= 0:
            continue
        cutoff = time.monotonic() - minutes * 60

        try:
            loaded = await client.loaded_models()
        except Exception:  # noqa: BLE001 — oMLX being briefly unreachable isn't fatal
            continue

        keep_warm = client.keep_warm()
        now = time.monotonic()
        for model in loaded:
            # Keep-warm models (the always-on summarizer) are exempt from
            # idle-unloading by design — the whole point is that they stay
            # resident for an instant response.
            if model in keep_warm:
                continue
            last = idle.last_used(model)
            if last is None:
                # First time seeing this model resident (e.g. loaded before
                # this loop started, or via a manual /models load) — start its
                # clock now rather than unloading it immediately.
                idle.touch(model)
                continue
            if idle.in_flight(model) > 0:
                # A generation is currently running on this model. Its last_used
                # clock only advances at request boundaries, so a single long
                # generation can age past the cutoff while still streaming —
                # unloading here is exactly the mid-stream eviction we must not
                # do. Leave it resident; it'll be reconsidered once idle.
                continue
            if last < cutoff:
                try:
                    await client.unload(model)
                except Exception:  # noqa: BLE001
                    continue
                idle.forget(model)
