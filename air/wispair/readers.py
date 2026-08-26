"""Parsing of the wire formats the Swift reader app pushes.

Both formats are pipe-delimited lines, newest-first, matching what the Pro's
readers already emit — kept identical on purpose so the Swift readers port
across with a changed URL rather than a changed format.

    email:    <epochSecs> | <account> | <sender name> | <subject>
    messages: <epochSecs> | <conversation label> | <who>: <text>

Two things that look like bugs and aren't:

**Scientific notation.** AppleScript emits the computed epoch as `1.783977044E+9`.
Python's `float()` parses that natively — no special handling needed, and none
should be added.

**Pipes inside the payload.** Subjects and message text can contain `|`, so
every parser here splits with a bounded `maxsplit` and treats the remainder as
one field. (Raw email *bodies* are a different matter — those use \\x01/\\x02
control characters precisely because a pipe format can't survive them — but
this node summarizes headers and message text, not raw bodies.)
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Field separator. A regex rather than a literal " | " because an empty field
# is common and real: when a message sits in a mailbox with no normal account,
# the AppleScript emits `ts & " | " & "" & " | " & sender`, which produces TWO
# spaces between the pipes. A fixed-string split silently mis-assigns every
# field after that point. `maxsplit` still protects pipes inside the payload.
_SEP = re.compile(r"\s*\|\s*")


def _dedupe_key(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8", "replace")).hexdigest()[:32]


def _epoch(raw: str) -> float | None:
    try:
        ts = float(raw.strip())
    except ValueError:
        return None
    # Sanity window: reject anything before 2001 or absurdly far in the future.
    # A malformed AppleScript date arithmetic result shows up as a wild number,
    # and letting one through would poison the cursor permanently (cursors are
    # monotonic — a bogus year-3000 timestamp could never be walked back).
    if ts < 978307200 or ts > 4102444800:
        return None
    return ts


def parse_email_headers(text: str) -> list[dict[str, Any]]:
    """`<epoch> | <account> | <sender> | <subject>` → item dicts."""
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = _SEP.split(line, maxsplit=3)
        if len(parts) < 4:
            continue
        ts = _epoch(parts[0])
        if ts is None:
            continue
        account, sender, subject = parts[1].strip(), parts[2].strip(), parts[3].strip()
        if not subject and not sender:
            continue
        items.append({
            "ts": ts,
            # Deliberately excludes `account`: the same message can be reported
            # under a slightly different account label between runs, and that
            # would defeat dedupe for no benefit.
            "dedupe_key": _dedupe_key(str(int(ts)), sender, subject),
            "payload": _render_email(ts, account, sender, subject),
        })
    return items


def _render_email(ts: float, account: str, sender: str, subject: str) -> str:
    acct = f" [{account}]" if account else ""
    return f"From {sender}{acct}: {subject}"


def parse_message_lines(text: str) -> list[dict[str, Any]]:
    """`<epoch> | <conversation> | <who>: <text>` → item dicts."""
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = _SEP.split(line, maxsplit=2)
        if len(parts) < 3:
            continue
        ts = _epoch(parts[0])
        if ts is None:
            continue
        context, body = parts[1].strip(), parts[2].strip()
        if not body:
            continue
        items.append({
            "ts": ts,
            "dedupe_key": _dedupe_key(str(int(ts)), context, body),
            "payload": f"[{context}] {body}",
        })
    return items


def newest_ts(items: list[dict[str, Any]]) -> float:
    return max((i["ts"] for i in items), default=0.0)
