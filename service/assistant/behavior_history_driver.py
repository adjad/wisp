"""One bounded explicit-history step, called only by the existing behavior tick.

No timer, task, transport, source observation or permission is created here on
construction. Source-owned clients are scoped to a single step, so withdrawal
and cancellation cannot leave an idle network client waiting for another tick.
"""
from __future__ import annotations

import asyncio
import math
import threading
import time

from service.gmail_behavior_history import StepBudget
from service.assistant.gmail_history_runtime import HistoryConflict, HistoryUnavailable

SPACING_SECONDS = 30
PENDING_RECORDS = 8


def _network_client():
    # Future runtime acquisition only, after current explicit Start admission.
    from service.gmail_source import HistoryHTTPClient
    return HistoryHTTPClient.network()


class HistoryDriver:
    def __init__(self, host, *, client_factory=None, client=None, monotonic=time.monotonic):
        if client_factory is not None and client is not None:
            raise ValueError("Choose an owned client factory or a borrowed client")
        self.host = host
        self._factory = client_factory or _network_client
        self._borrowed = client
        self._monotonic = monotonic
        self._last_dispatch = None
        self._last_account = None
        self._singleflight = threading.Lock()

    async def advance(self, *, pending=lambda account:0):
        if not self._singleflight.acquire(blocking=False):
            return dict(state="running")
        client = None
        owned = False
        selected = None
        try:
            jobs = [row for row in self.host.worker_jobs() if row["acquisition_pending"]]
            if not jobs:
                return dict(state="idle")
            now = self._monotonic()
            if (type(now) not in (int,float) or not math.isfinite(now)
                    or self._last_dispatch is not None and now-self._last_dispatch < SPACING_SECONDS):
                return dict(state="waiting")
            ids = [row["account_id"] for row in jobs]
            if self._last_account in ids:
                offset = (ids.index(self._last_account)+1) % len(jobs)
                jobs = jobs[offset:]+jobs[:offset]
            for row in jobs:
                count = pending(row["account_id"])
                if type(count) is int and 0 <= count < PENDING_RECORDS:
                    selected = row
                    break
            if selected is None:
                return dict(state="backpressure")
            self._last_dispatch = now  # Reserve before any client creation/await.
            self._last_account = selected["account_id"]
            client = self._borrowed
            if client is None:
                client = self._factory()
                owned = True
            result = await self.host.step(selected["account_id"],revision=selected["revision"],
                http=client,budget=StepBudget(100,5,6,400000,30))
            row = next((r for r in result["accounts"] if r["account_id"] == selected["account_id"]),None)
            if (row is None or row["state"] not in {"running","capped","exhausted"}
                    or row["counts"].get("failed",0) > 0):
                self.host.pause_job(selected["account_id"],selected["revision"],
                    "History paused after a source failure. Start again to resume with current permission.")
                return dict(state="paused")
            return dict(state="advanced",account_id=selected["account_id"])
        except asyncio.CancelledError:
            if selected is not None:
                self.host.pause_job(selected["account_id"],selected["revision"],"History work was cancelled.")
            raise
        except (HistoryConflict,HistoryUnavailable):
            return dict(state="withdrawn")
        except Exception:
            if selected is not None:
                self.host.pause_job(selected["account_id"],selected["revision"],
                    "History is unavailable. Refresh the connection before starting again.")
            return dict(state="unavailable")
        finally:
            try:
                if owned and client is not None:
                    try:
                        await asyncio.wait_for(client.aclose(),timeout=5)
                    except Exception:
                        if selected is not None:
                            self.host.pause_job(selected["account_id"],selected["revision"],
                                "History paused because its connection could not close cleanly.")
                        raise
            finally:
                self._singleflight.release()
