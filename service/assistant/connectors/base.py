"""Connector protocol — anything that produces commitments.

A connector is read-only: it reads an external source and returns a list of
commitment dicts. It never writes to the source and never raises out of poll()
(a failing connector = stale data, never a broken app). The scheduler calls
poll() on each connector's own interval and hands the result to the store.
"""
from __future__ import annotations

from typing import Protocol


class Connector(Protocol):
    name: str            # matches the commitment `source` column
    interval_s: float    # how often the scheduler polls this connector

    def available(self) -> tuple[bool, str]:
        """(ready, human-reason). False → the scheduler skips it and surfaces
        the reason in /assistant/status (e.g. 'Calendar access not granted')."""
        ...

    async def poll(self) -> list[dict]:
        """Return current commitments. Must not raise; return [] on any error."""
        ...
