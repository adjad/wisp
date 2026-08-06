"""Deterministic entity cards — who the user actually knows, computed by
joining Contacts against message activity. No model involved.

Exists because the LLM-extracted "People in their life" profile section
(memory/profile.py) is unreliable at the one thing that should be easiest:
`contacts.txt` already has every name Wisp needs, and per-contact message
counts are a cheap join, not something worth asking gpt-oss to notice inside a
16K-char batch of unrelated raw text. Verified failure: a real build (checked
2026-08-05) produced an EMPTY People section despite 100+ saved contacts and
hundreds of resolved messages sitting in the same cache the extractor read.

This module never replaces the model's output — see profile.py's
_augment_people_section — it only guarantees a factual floor: real contacts,
real counts, real dates, appended when the model's version is missing them.
"""
from __future__ import annotations

import time


def build_roster(limit: int = 40) -> list[dict]:
    """Saved contacts ranked by message activity: name, message count,
    first/last contact timestamp. Zero-activity contacts are still returned
    (count 0) so a caller can filter as it sees fit."""
    from service.tools.imessage_tools import contact_message_stats, contact_names

    stats = contact_message_stats()
    out = []
    for name in contact_names():
        s = stats.get(name)
        out.append({
            "name": name,
            "message_count": s["count"] if s else 0,
            "first_ts": s["first_ts"] if s else None,
            "last_ts": s["last_ts"] if s else None,
        })
    out.sort(key=lambda r: r["message_count"], reverse=True)
    return out[:limit]


def roster_lines(limit: int = 25, min_messages: int = 3) -> list[str]:
    """Ready-to-insert profile bullets for real, active contacts — tagged
    `[contacts+messages]` so a reader can tell these are joined from ground
    truth, not guessed from prose. `min_messages` excludes a saved contact who
    just happens to exist with no real activity behind them."""
    rows = [r for r in build_roster(limit) if r["message_count"] >= min_messages]
    lines = []
    for r in rows:
        first = time.strftime("%Y-%m", time.localtime(r["first_ts"]))
        last = time.strftime("%Y-%m-%d", time.localtime(r["last_ts"]))
        lines.append(f"- [contacts+messages] **{r['name']}** — {r['message_count']} "
                     f"messages, {first} to {last} (relationship not yet characterized)")
    return lines
