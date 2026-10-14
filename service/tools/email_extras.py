"""Inbox triage: subscriptions and priority sort.

Built on top of email_tools' existing `is_machine_sender`/`sender_stats` —
a real, working heuristic over the actual header history, rather than parsing
The native body cache does not supply List-Unsubscribe headers or historical
open events. Body links can only be offered for review, not treated as verified
one-click unsubscribe instructions.
"""
from __future__ import annotations

import re

from service.tools.email_tools import (
    _parse_history, _parse_lines, _parse_header_records,
    is_machine_sender, sender_digest, sender_stats,
)
from service.tools.registry import register


@register(
    "scan_subscriptions",
    "List newsletters/marketing senders cluttering the inbox, ranked by how "
    "often they email — the closest thing to 'what am I subscribed to' "
    "without a dedicated unsubscribe-tracking service. Counts and current "
    "unread flags do not establish that a sender's messages were never opened. "
    "Use unsubscribe to inspect a candidate link for review.",
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
    return (f"Top recurring senders ({len(subs)} found):\n" + "\n".join(lines)
            + "\nHistorical open events are unavailable; these counts do not prove "
              "that any sender's messages were never opened.")


@register(
    "triage_inbox",
    "Rank recent inbox senders using header-only subject, unread, account and "
    "recency signals. Show one note per sender address with original subjects; "
    "subject claims are unverified. Use for 'what actually matters in my inbox'.",
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
    from service.tools import email_tools
    state = email_tools.email_sync_state()
    if state == "syncing":
        return email_tools.email_syncing_message()
    if state == "unavailable":
        return email_tools._no_inbox_message()
    rows = _parse_header_records(email_tools._headers)
    if not rows:
        coverage = email_tools.header_scan_coverage([], raw_headers=email_tools._headers)
        if coverage["attempted"] or coverage["incomplete_accounts"]:
            names = ", ".join(coverage["incomplete_accounts"])
            incomplete = (f" Scan did not complete for {names}."
                          if coverage["incomplete_accounts"] else "")
            return (f"No parseable inbox headers. Native scan attempted {coverage['attempted']} "
                    f"and skipped {coverage['skipped']} malformed headers; coverage is incomplete."
                    f"{incomplete}")
        return "(No inbox data cached yet.)"
    selected = rows[:n]
    coverage = email_tools.header_scan_coverage(rows, raw_headers=email_tools._headers)
    return sender_digest(selected, "recent inbox triage", scanned=len(rows),
                         truncated=len(rows) - len(selected), max_senders=20,
                         scan_cap_accounts=coverage["cap_accounts"],
                         scan_incomplete_accounts=coverage["incomplete_accounts"],
                         scan_skipped=coverage["skipped"],
                         scan_attempted=coverage["attempted"])


