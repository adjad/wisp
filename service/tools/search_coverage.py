"""How far back Wisp can actually search — answered deterministically.

WHY THIS EXISTS AS A SEPARATE TOOL, NOT A SYSTEM-PROMPT INSTRUCTION
---------------------------------------------------------------------
It was one, first. "From how far back can you check emails" is a question
about WISP'S OWN REACH, and the fix looked like a pure prompting problem: tell
the model to answer capability questions directly instead of calling a data
tool and dumping results. That rule was added to SYSTEM and it did not hold —
replayed live TWICE against the real model after the prompt change, both times
it called `summarize_emails` again and answered with an inbox digest, never
stating a range.

That is not a reason to keep tuning the prose. This project's own established
pattern for "the model doesn't reliably do X from an instruction alone" is to
stop asking and make X the deterministic answer of a tool call instead (see
`conversions.calculate`, `get_battery_status`, `world_time` — the whole reason
those exist rather than leaving it to generation). The router direct-dispatches
this tool for the capability-question shape (see router.py's
`_SEARCH_COVERAGE_RE`) and its result is registered as a SHORT-CIRCUIT answer
(main.py's `_PRESYNTHESIZED_TOOLS`) — the model is never even called, so there
is nothing for it to get wrong.

WHERE THE NUMBERS COME FROM
----------------------------
Every figure here is read from the real constant that enforces it, not
independently maintained — see the inline citation on each line. If one of
those constants changes, this text goes stale silently unless whoever changes
it also greps for its value here; that is a known cost of not computing this
from a single shared source, accepted because the alternative (importing
Swift-side constants into Python at runtime) does not exist as a mechanism in
this codebase.
"""
from __future__ import annotations

from service.tools.registry import register

_COVERAGE = {
    # Cite the source: 730-day cutoff, MailReader.swift historyBatchScript
    # (raised from 365 -> 730 the same day this tool was written); verbatim
    # depth is view_emails' ~50-message cache.
    "email": (
        "Email: I can search and summarize headers going back about 2 years. "
        "Reading the exact wording of a message — the sender, recipient, full "
        "body — only reaches the ~50 most-recently-received; for anything "
        "older than that I can tell you it exists and what it's about, but "
        "not quote the exact text."
    ),
    # Cite: MessagesReader.swift readRecentMessages, 365-day / 20,000-row cap.
    "messages": (
        "Messages (iMessage/SMS): about 1 year back, up to roughly 20,000 "
        "messages."
    ),
    # Cite: assistant_tools.get_past_events, 365-day cap.
    "calendar": (
        "Calendar and reminders: up to about 1 year back, or however long "
        "Wisp has been syncing on this Mac if that's less."
    ),
    # Cite: notes_tools, ~100-note cache, no time bound.
    "notes": (
        "Notes: the ~100 most recently-edited notes, not bounded by time — an "
        "old note you haven't touched won't show up, one from years ago you "
        "edited yesterday will."
    ),
    # Cite: BrowserHistoryReader.swift, 30-day / 5,000-row cap per browser.
    "browser": (
        "Browser history: the last 30 days, up to about 5,000 entries per "
        "browser — page title, URL, and visit time only, never page content."
    ),
}


@register(
    "search_coverage",
    "State how far back Wisp can actually search a given source — email, "
    "messages, calendar, notes, or browser history. Use this for 'how far "
    "back can you check my X', 'how many Y can you search', 'what's the "
    "range on Z' — questions about WISP'S OWN REACH, not the user's data. "
    "Omit `source` to report on all of them. Do NOT call a data tool "
    "(summarize_emails etc.) to answer this kind of question — it answers a "
    "different question (what's actually there) and cannot state a range.",
    {
        "type": "object",
        "properties": {
            "source": {"type": "string",
                       "enum": ["email", "messages", "calendar", "notes", "browser"],
                       "description": "Which source to report on. Omit for all."},
        },
    },
    category="assistant_read",
    # Every alias here is verified to match _SEARCH_COVERAGE_RE (router.py) —
    # this tool's whole design is a DETERMINISTIC dispatch, not retrieval, so
    # an alias that only reaches it via the semantic fallback would silently
    # undermine the reason it exists. Checked live 2026-08-19 after two of the
    # original eight didn't actually match their own dispatch regex.
    aliases=[
        "from how far back can you check emails",
        "how far back can you search my messages",
        "how many months of email can you see",
        "how far back does your calendar go",
        "how far back do you go on my notes",
        "how much of my browsing history do you have",
        "what's your search range",
        "what's the range on your search",
    ],
)
def search_coverage(source: str = "") -> str:
    key = (source or "").strip().lower()
    if key and key not in _COVERAGE:
        return (f"(error: unknown source {key!r}. Use one of: "
                f"{', '.join(_COVERAGE)}, or omit for all.)")
    if key:
        return _COVERAGE[key]
    return "\n\n".join(_COVERAGE.values())
