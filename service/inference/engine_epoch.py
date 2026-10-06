"""Process-wide count of engine activity that can change which model is resident.

An operation that can load, unload or evict a model (a generation, a load or
unload, an embedding or rerank request, a connection reset) advances the count
when it starts AND again when it ends, and is counted as in flight in between.
Starting alone is not enough: the engine may apply the change after a status
read that began once the operation had started. Read-only status and health
reads never count, so background pollers do not invalidate anything.

service.inference.readiness stamps what it learned from a status read with the
count taken before the read and reuses it only while the count is unchanged and
nothing is in flight. Each generation through OMLXClient therefore adds exactly
two, which lets the turn tell its own generation from anyone else's. Anything
that talks to the engine without going through OMLXClient must wrap the call in
operation() itself.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

_lock = threading.Lock()
_epoch = 0
_in_flight = 0


def begin() -> None:
    global _epoch, _in_flight
    with _lock:
        _epoch += 1
        _in_flight += 1


def end() -> None:
    global _epoch, _in_flight
    with _lock:
        _epoch += 1
        _in_flight -= 1


@contextmanager
def operation():
    begin()
    try:
        yield
    finally:
        end()


def current() -> int:
    return _epoch


def quiet() -> bool:
    """True when no engine operation is running anywhere in this process."""
    return _in_flight == 0


def mark() -> int:
    """The count to stamp a status read with, taken just before the read.

    While an operation is in flight the read can show a half-applied change, so
    the mark is -1 and the proof it stamps never matches current().
    """
    with _lock:
        return _epoch if _in_flight == 0 else -1
