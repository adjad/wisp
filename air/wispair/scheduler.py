"""The 3-hour scheduler.

Implemented as a **60-second poll that asks "have we crossed a scheduled slot
since the last one we fired?"**, not as a sleep-until-next-run timer. That
distinction is the whole point: this machine sleeps 01:00-05:00 and its lid
gets closed unpredictably, and `asyncio.sleep()` does not advance across system
sleep. A timer-based scheduler would wake up hours late and, worse, would have
no idea it had missed anything.

The catch-up behaviour falls out for free: if the Air is dark through 05:00 and
the user opens it at 06:40, the most recent slot (05:00) is newer than the last
one fired (23:00), so the overnight run happens immediately on wake. Mail and
Messages backfill from their servers on wake, so by the time the run reads
them, the overnight content is actually there.

The last-fired slot is persisted, so a service restart can neither re-fire a
slot already done nor lose one that isn't.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from . import config, jobs, store

log = logging.getLogger("wispair.scheduler")

POLL_SECONDS = 60
_SLOT_KEY = "_last_fired_slot"

# A slot older than this is treated as missed-and-gone rather than fired late.
# Without it, a machine that was off for two days would wake and immediately
# summarize a two-day-old window as if it were current. The next run picks the
# content up anyway — items are never dropped, only the stale *trigger* is.
STALE_SLOT_HOURS = 6


def most_recent_slot(cfg: dict, now: float | None = None) -> float:
    """Timestamp of the latest scheduled slot at or before `now`."""
    now = now if now is not None else time.time()
    dt = datetime.fromtimestamp(now)
    hours = sorted(int(h) for h in cfg["run_hours"])
    today = [
        dt.replace(hour=h, minute=0, second=0, microsecond=0).timestamp()
        for h in hours
    ]
    passed = [t for t in today if t <= now]
    if passed:
        return max(passed)
    # Before the first slot of the day — the last slot of yesterday.
    yesterday = dt.replace(hour=hours[-1], minute=0, second=0, microsecond=0)
    return yesterday.timestamp() - 86400


async def _tick(cfg: dict) -> None:
    slot = most_recent_slot(cfg)
    last = store.get_cursor(_SLOT_KEY)
    if slot <= last:
        return                                  # already handled this slot

    age_h = (time.time() - slot) / 3600
    if age_h > STALE_SLOT_HOURS:
        log.info("skipping stale slot %s (%.1fh old)",
                 datetime.fromtimestamp(slot).strftime("%Y-%m-%d %H:%M"), age_h)
        store.set_cursor(_SLOT_KEY, slot)
        return

    # Record the slot BEFORE running, not after. A run that crashes must not
    # re-fire on the next 60s tick and crash again in a loop — the pending
    # items are still pending, so the next scheduled slot retries them anyway.
    store.set_cursor(_SLOT_KEY, slot)
    log.info("firing scheduled run for slot %s",
             datetime.fromtimestamp(slot).strftime("%Y-%m-%d %H:%M"))
    result = await jobs.run_all(cfg)
    log.info("run complete: %s", result)


async def loop() -> None:
    log.info("scheduler started (run_hours=%s)", config.load()["run_hours"])
    while True:
        try:
            await _tick(config.load())
        except asyncio.CancelledError:
            raise
        except Exception:                        # noqa: BLE001
            # Never let the scheduler die. This process is unattended for 20
            # hours a day; a crashed loop would be invisible until someone
            # noticed summaries had silently stopped.
            log.exception("scheduler tick failed")
        await asyncio.sleep(POLL_SECONDS)
