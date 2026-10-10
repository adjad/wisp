"""`uncaptured_commitment`: the one detector allowed to interrupt the user.

Fires when a text or email from someone the user is actually in contact with
states something coming up soon that is in neither Calendar nor Reminders.

The decision is a fixed sequence of gates. The first to say no wins and is
recorded as `blocked_by`, so a labelled miss in the report points at the gate
that caused it rather than at "the detector". Order is deliberate: the cheap,
safety-critical gates (promo/scam, contact) come before anything reads a time.

  1 direction    only incoming messages
  2 screen       promo / scam / automated never alert                (P3)
  3 contact      recent two-way contact with the sender              (P4)
  4 resolve      a concrete time, read the way a person reads a chat
  5 soon         starts within `soon_hours` of arrival
  6 confirmed    a question or guessed meridiem needs the user's own "yes" in reply
  7 on_file      not already in Calendar or Reminders

Pure and deterministic. Takes a `Snapshot` (frozen data) and never reads a live
source, a clock, or a model.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from service.attention.contacts import Contacts
from service.attention.corpus import Item, Snapshot
from service.attention.prediction import Prediction
from service.attention.exclusions import screen
from service.attention.matching import find_on_file
from service.attention.resolve import Resolved, resolve

CODE = "uncaptured_commitment"
SOON_HOURS = 48.0
REPLY_WINDOW_S = 6 * 3600
# A date with no clock time ("tomorrow", "Friday") alerts only when the sentence also states
# something the user must do or attend. Without this, "your order arrives tomorrow" and
# "check your stocks today" were 11 of the first 13 false alerts on real data.
_OBLIGATION = re.compile(
    r"(?<!nothing )(?<!no )\b(?:due|deadline|submit|turn\s+in|hand\s+in|don'?t\s+forget|"
    r"remember\s+to|reminder|need\s+to|have\s+to|must|pick\s+up|appointment|exam|quiz|"
    r"midterm|final|interview|meeting|rsvp|sign\s+up|register|expires?|closes?)\b", re.I)
_DECLINE = re.compile(r"\b(?:can'?t|cannot|won'?t|no|nope|sorry|not\s+able|unable|rain\s*check|"
                      r"pass|busy)\b", re.I)


def local_timezone() -> str:
    """The Mac's IANA zone, from /etc/localtime. Falls back to UTC if it can't tell."""
    try:
        path = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in path:
            return path.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


@dataclass(frozen=True)
class Decision:
    alert: bool
    blocked_by: str | None = None
    resolved: Resolved | None = None
    importance: float = 0.0
    why: tuple[str, ...] = ()


class UncapturedCommitment:
    def __init__(self, snapshot: Snapshot, tz: str | None = None, soon_hours: float = SOON_HOURS):
        self.snapshot = snapshot
        self.tz = tz or local_timezone()
        self.soon = timedelta(hours=soon_hours)
        self.contacts = Contacts(snapshot.items)

    def _user_confirmed(self, item: Item) -> bool:
        """The user wrote back in the same thread soon after, and not to decline."""
        if item.source != "messages":
            return False
        later = [i for i in self.snapshot.items
                 if i.source == "messages" and i.conversation == item.conversation
                 and i.direction == "outgoing" and item.ts < i.ts <= item.ts + REPLY_WINDOW_S]
        return bool(later) and not any(_DECLINE.search(i.text) for i in later)

    def decide(self, item: Item) -> Decision:
        if item.direction != "incoming":
            return Decision(False, "not_incoming")
        verdict = screen(item)
        if verdict:
            return Decision(False, verdict)
        imp = self.contacts.importance(item)
        if not imp.eligible:
            return Decision(False, imp.reason, importance=imp.score)
        arrival = datetime.fromtimestamp(item.ts, timezone.utc)
        r = resolve(item.text, arrival, self.tz, item.id)
        if r.blocked or r.day is None:
            return Decision(False, r.blocked or "no_usable_time", r, imp.score)

        if not r.has_clock and not _OBLIGATION.search(item.text):
            return Decision(False, "date_only_no_obligation", r, imp.score)

        if r.has_clock and r.start is not None:
            soon = arrival <= r.start <= arrival + self.soon
        else:   # date-only: soon if the day falls inside the window, counted in calendar days
            local_day = arrival.astimezone(ZoneInfo(self.tz)).date()
            soon = 0 <= (r.day - local_day).days <= int(self.soon.total_seconds() // 86400)
        if not soon:
            return Decision(False, "not_soon", r, imp.score)

        if r.tentative and not self._user_confirmed(item):
            return Decision(False, "unconfirmed", r, imp.score)

        match = find_on_file(r, item.text, self.snapshot.commitments, self.tz)
        if match:
            return Decision(False, "already_on_file", r, imp.score,
                            (f"{match.basis}: {match.title}",))
        why = (f"stated: {r.quote}", f"contact: {imp.reason}") + tuple(f"inferred {x}" for x in r.inferred)
        return Decision(True, None, r, imp.score, why)

    def __call__(self, item: Item, snapshot: Snapshot) -> Prediction:
        d = self.decide(item)
        return Prediction(d.alert, CODE if d.alert else None, d.blocked_by)


_cache: dict[int, tuple[Snapshot, UncapturedCommitment]] = {}


def uncaptured_commitment(item: Item, snapshot: Snapshot) -> Prediction:
    """Registry entry: builds (and remembers) one detector per snapshot."""
    entry = _cache.get(id(snapshot))
    if entry is None or entry[0] is not snapshot:
        entry = _cache[id(snapshot)] = (snapshot, UncapturedCommitment(snapshot))
    return entry[1](item, snapshot)
