"""Is this already on the user's calendar or reminders?

This is the half of the notification rule that protects the user from duplicates,
so it errs toward "known" (no alert): a near-match in time with any shared
meaningful word, or an almost exact time with no shared words at all, counts as
already on file. Missing a real reminder is recoverable (the message is still in
Messages); a redundant interruption teaches the user to ignore Wisp.

Only `active` and `done` rows count. A `dismissed` row is something the user
cleared, not something they have on file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from service.attention.resolve import Resolved

CLOCK_EXACT_S = 30 * 60         # this close in time: known even with no words in common
CLOCK_NEAR_S = 120 * 60         # this close, plus a shared word: known

_STOP = frozenset("""
the and for with that this from have has are was were you your our their they them
will would could should can cant cannot not but just now then than too very
tomorrow today tonight morning afternoon evening noon night
monday tuesday wednesday thursday friday saturday sunday
let lets going gonna wanna come get got see
""".split())
_WORD = re.compile(r"[a-z0-9]{3,}")


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").casefold()) if w not in _STOP and not w.isdigit()}


@dataclass(frozen=True)
class OnFile:
    commitment_id: str
    title: str
    basis: str                  # "time" | "time+words" | "day+words"


def find_on_file(resolved: Resolved, text: str, commitments: list[dict], tz: str) -> OnFile | None:
    if resolved.day is None:
        return None
    zone = ZoneInfo(tz)
    words = _tokens(text)
    for c in commitments:
        if c.get("status") not in ("active", "done") or c.get("when_ts") is None:
            continue
        when = datetime.fromtimestamp(float(c["when_ts"]), zone)
        shared = bool(words & _tokens(f"{c.get('title') or ''} {c.get('location') or ''}"))
        if resolved.has_clock and resolved.start is not None:
            gap = abs(when.timestamp() - resolved.start.timestamp())
            if gap <= CLOCK_EXACT_S:
                return OnFile(str(c.get("id")), str(c.get("title")), "time")
            if gap <= CLOCK_NEAR_S and shared:
                return OnFile(str(c.get("id")), str(c.get("title")), "time+words")
        elif when.date() == resolved.day and shared:
            return OnFile(str(c.get("id")), str(c.get("title")), "day+words")
    return None
