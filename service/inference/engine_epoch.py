"""Process-wide count of engine activity that can change which model is resident.

The count goes up before any request that can load, unload or evict a model:
a generation, a load or unload, an embedding or rerank request (the engine
loads those models on demand), and a connection reset. Read-only status and
health reads never count, so background pollers do not invalidate anything.

service.inference.readiness stamps what it learned from a status read with the
count taken before the read and reuses it only while the count is unchanged.
Each generation through OMLXClient adds exactly one, which lets the turn tell
its own generation from anyone else's. Anything that talks to the engine
without going through OMLXClient must call bump() itself.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_epoch = 0


def bump() -> None:
    global _epoch
    with _lock:
        _epoch += 1


def current() -> int:
    return _epoch
