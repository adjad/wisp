"""What changed recently, across every app at once.

The question "is there anything new?" has no single source — a new text, a new
email, a reminder someone just added and a note edited this morning are four
different caches with four different shapes. Answering it used to mean the model
calling three or four read tools and merging them itself, which is slow (each
summarizer makes its own model call) and unreliable (it often stopped after the
first source that returned something).

This does the merge in Python instead: one cheap, NO-MODEL call that reads the
caches already in memory and returns a single recency-ordered feed, labelled by
source. It is the fast front door for "what's new / anything I missed / what's
the latest"; the per-source tools stay the right choice once the user knows
which app they care about.

TWO DESIGN RULES worth stating, because both were learned from real failures:

  * NOTES IS WEIGHTED LAST. The user keeps very little in Notes, so a note
    edit is the least interesting thing here and must never crowd out a real
    message or email. It gets its own small quota rather than competing on
    recency alone.

  * AN EMPTY WINDOW WIDENS ITSELF instead of dead-ending. `summarize_emails`
    shipped the opposite behaviour and it produced a wrong answer in the field:
    asked at 2:32am on a Monday, `period="this week"` covered 2.5 hours, matched
    nothing, and the model told the user they had no email at all. A recency
    feed is even more exposed to that — "the last 24 hours" is empty at 6am
    after a quiet night — so when the window comes back empty this widens and
    says so, rather than reporting "nothing new".
"""
from __future__ import annotations

import time
from datetime import datetime

from service.tools.registry import register

# Widening ladder, in hours. Starts at the caller's window and grows until
# something turns up — see the module docstring on why an empty window must not
# be a dead end.
_WIDEN_STEPS = (24, 72, 168, 720)

# Most note edits that may appear regardless of how recent they are. Notes is
# the weakest source for this user, and a burst of edits would otherwise push
# real messages and mail out of the list entirely.
_NOTES_QUOTA = 3

# Per-source caps. These are deliberately small and per-SOURCE rather than one
# shared budget: verified on real data, a single chatty group thread ("Comp",
# 12 messages in two hours) filled every slot and the 7-day feed contained no
# email at all — which is the exact opposite of what a cross-app view is for.
_CAP_MESSAGES = 6      # conversations, not messages — see _collect
_CAP_EMAIL = 6
_CAP_COMMITMENTS = 5


def _ago(ts: float, now: float) -> str:
    """Human gap, chosen so the model can quote it directly."""
    s = max(0, int(now - ts))
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        h = s // 3600
        return f"{h} hour{'s' if h > 1 else ''} ago"
    d = s // 86400
    return f"{d} day{'s' if d > 1 else ''} ago"


def _clip(text: str, n: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _collect(since: float, now: float) -> list[dict]:
    """Every source's rows since `since`, as {ts, source, line}.

    Each source is read defensively: a cache that hasn't synced, or a module
    that fails to import, must degrade to "that source contributed nothing"
    rather than failing the whole feed — this tool's entire value is that it
    answers from whatever IS available.
    """
    items: list[dict] = []

    # --- Messages (Messages.app: iMessage/SMS) --------------------------------
    try:
        from service.tools import imessage_tools
        rows = [r for r in imessage_tools._parse_lines() if r[0] >= since]
        rows.sort(key=lambda r: r[0], reverse=True)
        # ONE LINE PER CONVERSATION, not per message. A group thread produces
        # dozens of lines in an evening, and listing them individually both
        # buries the other apps and tells the user nothing they didn't already
        # know ("Comp is busy"). The newest message plus a count is the actual
        # answer to "anything new?".
        by_convo: dict[str, list] = {}
        for ts, context, text in rows:
            by_convo.setdefault(context, []).append((ts, text))
        convos = sorted(by_convo.items(), key=lambda kv: kv[1][0][0], reverse=True)
        for context, msgs in convos[:_CAP_MESSAGES]:
            ts, text = msgs[0]
            more = f"  (+{len(msgs) - 1} more)" if len(msgs) > 1 else ""
            items.append({"ts": ts, "source": "MESSAGES",
                          "line": f"{context} — {_clip(text)}{more}"})
    except Exception:  # noqa: BLE001 — one dead source must not kill the feed
        pass

    # --- Mail -----------------------------------------------------------------
    try:
        from service.tools import email_tools
        rows = [r for r in email_tools._parse_lines() if r[0] >= since]
        rows.sort(key=lambda r: r[0], reverse=True)
        for ts, _account, sender, subject, unread in rows[:_CAP_EMAIL]:
            # Only SAY unread when it's actually known — None means the cached
            # line predates the read-status field, and calling that "read"
            # would be a confident guess (see email_tools._parse_pipe_lines).
            tag = "(unread) " if unread else ""
            items.append({"ts": ts, "source": "EMAIL",
                          "line": f"{tag}{sender} — {_clip(subject)}"})
    except Exception:  # noqa: BLE001
        pass

    # --- Reminders + calendar events Wisp first saw in the window -------------
    try:
        from service.assistant.store import assistant_store
        for row in assistant_store.recently_added(since, limit=_CAP_COMMITMENTS):
            when = row.get("when_ts")
            due = (datetime.fromtimestamp(when).strftime("%a %b %-d, %-I:%M %p")
                   if when else "no date")
            kind = (row.get("kind") or "event").upper()
            items.append({"ts": float(row.get("created_at") or 0),
                          "source": "REMINDER" if kind == "REMINDER" else "CALENDAR",
                          "line": f"added: {_clip(row.get('title') or '(untitled)')} "
                                  f"— due {due}"})
    except Exception:  # noqa: BLE001
        pass

    # --- Notes (LAST, and quota'd — see the module docstring) -----------------
    try:
        from service.tools import notes_tools
        rows = [n for n in notes_tools._parse() if n["ts"] >= since]
        rows.sort(key=lambda n: n["ts"], reverse=True)
        for n in rows[:_NOTES_QUOTA]:
            body = _clip(n.get("body") or "", 80)
            items.append({"ts": n["ts"], "source": "NOTES",
                          "line": f"edited: {n.get('title') or '(untitled)'}"
                                  + (f" — {body}" if body else "")})
    except Exception:  # noqa: BLE001
        pass

    return items


def _span(window_h: int) -> str:
    if window_h < 48:
        return "the last hour" if window_h == 1 else f"the last {window_h} hours"
    d = window_h // 24
    return "the last day" if d == 1 else f"the last {d} days"


def _render(items: list[dict], now: float, window_h: int,
            asked_h: int | None = None) -> str:
    items.sort(key=lambda i: i["ts"], reverse=True)
    span = _span(window_h)
    head = f"Most recent activity across your apps ({span}), newest first:"
    # Name the window the caller ASKED for, not a hardcoded 24h — otherwise a
    # widen from 1h to 24h reads "Nothing in the last 24 hours, so this covers
    # the last 24 hours instead", which is nonsense.
    if asked_h is not None and asked_h != window_h:
        head = (f"Nothing in {_span(asked_h)}, so this covers {span} instead "
                f"— say so rather than reporting that nothing has happened:")
    lines = [f"[{_ago(i['ts'], now):>11}]  {i['source']:<9} {i['line']}"
             for i in items]
    return head + "\n\n" + "\n".join(lines)


@register(
    "get_recent_activity",
    "What's NEW across all of the user's apps at once — recent texts, emails, "
    "newly added reminders/events, and edited notes, merged into one list "
    "newest-first. Call this for 'what's new?', 'anything I missed?', 'what's "
    "the latest?', 'catch me up', 'anything happen while I was out?' — "
    "questions that don't name one app. It is a fast, direct read of what "
    "already synced (no summarizing), so prefer it over calling the individual "
    "read tools one by one just to find out where something new turned up. "
    "Once you know WHICH app the user cares about, use that app's own tool for "
    "depth: summarize_messages / summarize_emails / get_upcoming / "
    "search_notes.",
    {"type": "object",
     "properties": {
         "hours": {"type": "integer",
                   "description": "how far back to look (default 24). If nothing "
                                  "is found it widens automatically."},
         "limit": {"type": "integer",
                   "description": "max items to return (default 20)"},
     }},
    category="assistant_read",
)
async def get_recent_activity(hours: int = 24, limit: int = 20) -> str:
    now = time.time()
    start = max(1, int(hours or 24))
    # Try the asked-for window, then widen. See the module docstring: an empty
    # window is the single most likely way this tool would produce a confidently
    # wrong "nothing's happened".
    steps = [h for h in _WIDEN_STEPS if h > start]
    for i, window in enumerate([start, *steps]):
        items = _collect(now - window * 3600, now)
        if items:
            return _render(items[: max(1, limit)], now, window,
                           asked_h=start if i > 0 else None)
    return ("Nothing new turned up in any app going back a month — messages, "
            "mail, reminders and notes are all quiet. (If that seems wrong, the "
            "caches may still be syncing after launch.)")
