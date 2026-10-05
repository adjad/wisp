"""Conservative typo/shorthand normalization for the router's fall-through path.

Only used AFTER every rule has declined the original text (see
router._route_request), so it can never pre-empt an existing route: it gives
the same tested rules one more look at "whats on my calender tmrw" or
"emial alex that the meeting moved" before the request falls to the generic
retrieved menu.

Entries are unambiguous misspellings or chat shorthand only. Deliberately
absent: words that are also names or real words ("cal", "whether", "u").
"""
from __future__ import annotations

import re

_TYPOS: tuple[tuple[str, str], ...] = (
    (r"tmrw|tmr|tmrow|tmw|tommorow|tomorow|tommorrow|tomorrw|2moro|2morrow", "tomorrow"),
    (r"tdy|2day", "today"),
    (r"calender|calandar|calander|calendr", "calendar"),
    (r"schedual|scedule|shedule|schedul", "schedule"),
    (r"emial|emali|e-mial|emal", "email"),
    (r"emials|emals", "emails"),
    (r"txt", "text"),
    (r"txts", "texts"),
    (r"remeber|rember|remmeber|remembr", "remember"),
    (r"rmind|remnd|remid|remine", "remind"),
    (r"remiders|remindrs|reminers", "reminders"),
    (r"serch|seach|srch", "search"),
    (r"chek|chk", "check"),
    (r"drft", "draft"),
    (r"mesage|messege|msg", "message"),
    (r"msgs|mesages", "messages"),
    (r"appt", "appointment"),
    (r"appts", "appointments"),
    (r"pls|plz", "please"),
)
_COMPILED = tuple((re.compile(rf"\b(?:{pattern})\b", re.I), word) for pattern, word in _TYPOS)


def normalize_typos(text: str) -> str:
    """Return `text` with known misspellings/shorthand replaced (else unchanged)."""
    out = text
    for pattern, word in _COMPILED:
        out = pattern.sub(word, out)
    return out
