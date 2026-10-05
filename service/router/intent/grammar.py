"""Subject-free personal agenda grammar shared by reads and the web boundary."""
from __future__ import annotations
import re

_SCOPE = r"(?:today|tomorrow|tonight|this week|next week|this weekend|next weekend|this month|next month)"
_AGENDA = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+)?(?:"
    r"what(?:['’]s|\s+is|\s+are)\s+(?:up|on|happening|going on|planned|scheduled|coming up)"
    r"|what do (?:i|we) have(?:\s+(?:on|planned|scheduled))?"
    r"|(?:show|check|list)\s+(?:me\s+)?(?:my\s+)?(?:agenda|schedule|calendar))"
    r"\s+(?:(?:for|on|in|during)\s+)?(?:the\s+)?(?P<scope>" + _SCOPE + r")[.!?]*$", re.I)


def personal_agenda_period(text: str) -> str | None:
    """Only complete questions with no public subject, location or extra clause."""
    match = _AGENDA.fullmatch(re.sub(r"\s+", " ", text.strip()))
    return match.group("scope").lower() if match else None


def flexible_personal_agenda(text: str) -> bool:
    """Eligibility only, not a compiled date/tool decision.

    Subject-free variants and explicit personal week questions enter validation;
    public locations, topics and action clauses continue the existing boundary.
    """
    t = re.sub(r"\s+", " ", text.strip().lower()).strip(" .!?")
    return bool(re.fullmatch(
        r"(?:whats|what(?:['’]s| is)) (?:up|on|coming up) (?:for )?(?:the )?(?:week|wk)"
        r"|how(?:['’]s| is) (?:my|our) (?:week|wk|month|schedule|calendar|calender) (?:looking|shaping up)"
        r"|(?:is there |do i have )?anything (?:coming up|on|planned|scheduled)(?: for me)?"
        r"|(?:my|our) (?:calendar|calender|cal|schedule|agenda)(?: (?:this|next) (?:week|wk|month))?", t))
