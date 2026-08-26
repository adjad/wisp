"""Inbox triage: subscriptions, thread summaries, priority sort, unsubscribe.

Built on top of email_tools' existing `is_machine_sender`/`sender_stats` —
a real, working heuristic over the actual header history, rather than parsing
List-Unsubscribe headers (which only the ~50-message raw cache has access to,
per email_tools' own documented cache split — too shallow to answer "what am
I subscribed to" honestly across the full 2-year window).
"""
from __future__ import annotations

import re

from service.tools.email_tools import (
    _parse_history, _parse_lines, is_machine_sender, sender_stats,
)
from service.tools.registry import register


@register(
    "scan_subscriptions",
    "List newsletters/marketing senders cluttering the inbox, ranked by how "
    "often they email — the closest thing to 'what am I subscribed to' "
    "without a dedicated unsubscribe-tracking service. Use unsubscribe to act "
    "on one.",
    {"type": "object",
     "properties": {
         "limit": {"type": "integer", "description": "How many senders to list. Default 15."},
     }},
    category="email_read",
    aliases=["what am I subscribed to", "clean up my newsletter subscriptions",
             "what marketing emails am I getting", "who's spamming my inbox",
             "show me my recurring senders"],
)
def scan_subscriptions(limit: int = 15) -> str:
    try:
        n = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        n = 15
    stats = sender_stats()
    subs = [(sender, s) for sender, s in stats.items() if is_machine_sender(sender)]
    if not subs:
        return "No recurring newsletter/marketing senders found in the synced history."
    subs.sort(key=lambda kv: kv[1]["count"], reverse=True)
    lines = [f"  {sender} — {s['count']} emails" for sender, s in subs[:n]]
    return f"Top recurring senders ({len(subs)} found):\n" + "\n".join(lines)


@register(
    "triage_inbox",
    "Sort the recent inbox into ACTUALLY NEEDS ATTENTION vs. can-wait/"
    "automated — a quick priority pass rather than a full summary. Use for "
    "'what actually matters in my inbox' or 'what needs a reply'.",
    {"type": "object",
     "properties": {"count": {"type": "integer", "description": "How many recent emails to scan. Default 40."}}},
    category="email_read",
    aliases=["what actually matters in my inbox", "what needs my attention",
             "sort my inbox by priority", "what can I ignore in my email"],
)
def triage_inbox(count: int = 40) -> str:
    try:
        n = max(5, min(200, int(count)))
    except (TypeError, ValueError):
        n = 40
    rows = _parse_lines()[:n]
    if not rows:
        return "(No inbox data cached yet.)"
    priority, routine = [], []
    for ts, account, sender, subject, unread in rows:
        (routine if is_machine_sender(sender) else priority).append((sender, subject, unread))
    out = []
    if priority:
        out.append(f"Needs a look ({len(priority)}):")
        out += [f"  {'[unread] ' if u else ''}{s} — {subj}"
               for s, subj, u in priority[:20]]
    if routine:
        out.append(f"\nRoutine/automated ({len(routine)}, not shown individually).")
    return "\n".join(out) if out else "Nothing in the scanned range."


@register(
    "summarize_thread",
    "Summarize everything in ONE email thread/conversation — every message in "
    "it, not just the latest. Use when the user asks about 'the whole thread' "
    "or 'what's been said back and forth' rather than a single message.",
    {"type": "object",
     "properties": {
         "subject": {"type": "string", "description": "The thread's subject line (or a distinctive part of it)."},
     },
     "required": ["subject"]},
    category="email_read",
    aliases=["summarize the whole email thread", "recap this whole email chain",
             "what's been said in this email chain"],
)
def summarize_thread(subject: str) -> str:
    needle = re.sub(r"^(re|fwd?):\s*", "", (subject or "").strip().lower())
    if not needle:
        return "(error: summarize_thread needs a `subject`.)"
    rows = _parse_history() or _parse_lines()
    matches = [r for r in rows
              if needle in re.sub(r"^(re|fwd?):\s*", "", (r[3] or "").lower())]
    if not matches:
        return f"No thread found matching {subject!r}."
    matches.sort(key=lambda r: r[0])
    lines = []
    for ts, account, sender, subj, unread in matches[:30]:
        import datetime
        when = datetime.datetime.fromtimestamp(ts).strftime("%b %-d, %-I:%M%p")
        lines.append(f"  {when} — {sender}: {subj}")
    return f"Thread {matches[0][3]!r} ({len(matches)} messages):\n" + "\n".join(lines)


@register(
    "unsubscribe",
    "Unsubscribe from a sender's mailing list, using the List-Unsubscribe "
    "info in their most recent email if it's still in the raw cache (recent "
    "~50 messages only). If it's not available, this says so and archives "
    "the message instead — it never guesses a link to visit.",
    {"type": "object",
     "properties": {"sender": {"type": "string", "description": "The sender to unsubscribe from, by name or address."}},
     "required": ["sender"]},
    category="email_triage",
    aliases=["unsubscribe from this newsletter", "stop these marketing emails",
             "get me off this mailing list"],
)
def unsubscribe(sender: str) -> str:
    from service.tools.email_tools import _raw_emails

    who = (sender or "").strip().lower()
    if not who:
        return "(error: unsubscribe needs a `sender`.)"
    if not _raw_emails:
        return ("(No raw email cache available right now, so I can't find an "
                "unsubscribe link. Try again once Mail has synced, or search "
                "for the sender's email and open its unsubscribe link "
                "yourself.)")
    # Raw cache is FS/RS-delimited per email_tools' own format; find the
    # newest record from this sender and look for a URL near "unsubscribe" in
    # its raw content.
    for record in reversed(_raw_emails.split("\x02")):
        fields = record.split("\x01")
        if len(fields) < 7:
            continue
        sender_field = fields[3].lower()
        if who not in sender_field:
            continue
        body = fields[6]
        m = re.search(r"https?://[^\s\"'<>]*unsubscribe[^\s\"'<>]*", body, re.I)
        if m:
            return (f"Found an unsubscribe link for {fields[3]}: {m.group(0)}\n"
                    f"Open it in a browser to complete it — Wisp won't click "
                    f"links on your behalf without you seeing where it goes first.")
        return (f"No unsubscribe link found in the cached email from "
                f"{fields[3]}. Try archive_email to at least clear it from "
                f"your inbox, or check the email yourself for an unsubscribe option.")
    return (f"No recent email from a sender matching {sender!r} in the raw "
            f"cache (last ~50 messages). Try again after the sender's most "
            f"recent email, or search your inbox for it directly.")
