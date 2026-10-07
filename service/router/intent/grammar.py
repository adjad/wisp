"""Subject-free personal agenda grammar shared by reads and the web boundary."""
from __future__ import annotations
import re
from datetime import datetime

_SCOPE = r"(?:today|tomorrow|tonight|this week|next week|this weekend|next weekend|this month|next month)"
_AGENDA = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+)?(?:"
    r"what(?:['’]s|s|\s+is|\s+are)\s+(?:up|on|happening|going on|planned|scheduled|coming up)"
    r"|what do (?:i|we) have(?:\s+(?:on|planned|scheduled))?"
    r"|(?:show|check|list)\s+(?:me\s+)?(?:my\s+)?(?:agenda|schedule|calendar))"
    r"\s+(?:(?:for|on|in|during)\s+)?(?:the\s+)?(?P<scope>" + _SCOPE + r")[.!?]*$", re.I)


def normalize_personal_agenda(text: str) -> str:
    """Correct one known scope typo only in a complete subject-free agenda.

    Equal-length replacement preserves offsets. Quoted queries, public subjects
    and extra instructions do not match this grammar and keep their bytes.
    """
    candidate = re.sub(r"\bweej\b", "week", text, flags=re.I)
    return candidate if _AGENDA.fullmatch(re.sub(r"\s+", " ", candidate.strip())) else text


def personal_agenda_period(text: str) -> str | None:
    """Only complete questions with no public subject, location or extra clause."""
    match = _AGENDA.fullmatch(re.sub(r"\s+", " ", normalize_personal_agenda(text).strip()))
    return match.group("scope").lower() if match else None


def personal_agenda_args(text: str, *, now: datetime | None = None) -> dict | None:
    """Complete agenda arguments shared by orchestration and read shortcuts.

    An explicit calendar names one source. Existing subject-free day planning
    also means Calendar only; broad weekly/monthly agendas retain reminders.
    """
    period = personal_agenda_period(text)
    if period is None:
        return None
    from .compiler import canonical_period
    from .schema import TimeScope
    args = {"period": canonical_period(TimeScope(named=period), now=now)}
    explicit_calendar = bool(re.search(r"\bcalendar\b", text, re.I))
    explicit_agenda = bool(re.search(r"\b(?:agenda|schedule)\b", text, re.I))
    if explicit_calendar or (period in {"today", "tomorrow"} and not explicit_agenda):
        args["calendar_only"] = True
    return args


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
