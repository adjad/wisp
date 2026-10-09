"""How important is this sender right now? Recency of two-way contact (user decision P4).

"Who I texted last, or who messaged me last." No VIP list and no model: the signal
is the Messages feed itself, where `direction` says who spoke.

The score is computed as of the moment the message arrived, never from later
messages, so a replay over a frozen snapshot cannot leak the future into the
past. (A conversation the user only started replying to AFTER a message arrived
does not make that earlier message important.)

Mail is the weak case: the Sent mailbox is not cached, so two-way contact cannot
be shown from mail alone. A mail sender counts only when its display name matches
someone the user has actually exchanged texts with.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

from service.attention.corpus import Item

DAY = 86400.0
HALF_LIFE_DAYS = 14.0
ELIGIBLE_SCORE = 0.1            # roughly: the user wrote to them in the last ~46 days
GROUP_INCOMING_MIN = 3          # ...or they wrote to the user at least this often, recently
GROUP_WINDOW_DAYS = 30.0


@dataclass(frozen=True)
class Importance:
    score: float
    eligible: bool
    reason: str                 # "recent_outgoing" | "frequent_incoming" | "mail_name_match" | why not


def _decay(days: float) -> float:
    return 0.5 ** (days / HALF_LIFE_DAYS)


class Contacts:
    def __init__(self, items: list[Item]):
        self._out: dict[str, list[float]] = {}
        self._in: dict[str, list[float]] = {}
        self._texted: set[str] = set()
        for it in sorted(items, key=lambda i: i.ts):
            if it.source != "messages":
                continue
            (self._out if it.direction == "outgoing" else self._in).setdefault(
                it.conversation, []).append(it.ts)
        # Names the user has actually exchanged texts with, for matching mail senders.
        for it in items:
            if it.source == "messages" and it.conversation in self._out:
                self._texted.add(it.conversation.casefold())
                if it.direction == "incoming" and it.sender:
                    self._texted.add(it.sender.casefold())

    def importance(self, item: Item) -> Importance:
        if item.source == "mail":
            name = (item.sender or "").casefold()
            if name and name in self._texted:
                return Importance(0.3, True, "mail_name_match")
            return Importance(0.0, False, "unknown_mail_sender")
        conv = item.conversation
        outs = self._out.get(conv, [])
        ins = self._in.get(conv, [])
        # Only messages strictly earlier than this one count.
        n_out = bisect_left(outs, item.ts)
        last_out = outs[n_out - 1] if n_out else None
        recent_in = (bisect_left(ins, item.ts)
                     - bisect_left(ins, item.ts - GROUP_WINDOW_DAYS * DAY))
        if last_out is not None:
            score = _decay((item.ts - last_out) / DAY)
            if score >= ELIGIBLE_SCORE:
                return Importance(round(score, 3), True, "recent_outgoing")
        if recent_in >= GROUP_INCOMING_MIN:
            return Importance(round(min(0.5, 0.1 * recent_in), 3), True, "frequent_incoming")
        return Importance(0.0, False, "no_recent_two_way_contact")
