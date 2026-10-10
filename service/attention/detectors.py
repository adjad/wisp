"""Rule-based detector for the one notifying reason code: ``uncaptured_commitment``.

A person texts something that is coming up soon ("Meet me in the Quad at 6PM"),
it is in neither Calendar nor Reminders, and it is not promotional. Pure code, no
model, no I/O: callers pass messages, the clock and the commitments on file.

Fail closed. Anything ambiguous (no am/pm, a question, a hedge, two competing
times, a weekday name, a promo/scam shape, an unresolvable sender) yields no
candidate rather than a confident alert. Every candidate carries a verbatim quote
that is a substring of the message, located by code.

The dates it resolves are deliberately narrow: a clock time with an explicit
meridiem (or a 24-hour time past 12), optionally with "today"/"tonight"/"tomorrow".
With no day word, the next occurrence of that time after the message arrived.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from service.attention.corpus import Item

REASON = "uncaptured_commitment"
HORIZON_S = 24 * 3600          # "soon"
KNOWN_NEAR_S = 15 * 60         # a commitment this close is the same thing
KNOWN_RELATED_S = 60 * 60      # ...or this close when it shares a content word

_AMPM = re.compile(r"(?<![\w:])(?P<h>1[0-2]|0?[1-9])(?::(?P<m>[0-5]\d))?\s*(?P<ap>[ap])\.?m\.?(?![a-z])", re.I)
_H24 = re.compile(r"(?<![\w:])(?P<h>1[3-9]|2[0-3]|0\d):(?P<m>[0-5]\d)(?![\w:])")
_NOON = re.compile(r"\bnoon\b", re.I)
_TOMORROW = re.compile(r"\b(?:tomorrow|tmrw|tmr)\b", re.I)
_TODAY = re.compile(r"\b(?:today|tonight|this\s+(?:morning|afternoon|evening))\b", re.I)
# A day we do not resolve (weekday, month/day, "next week"): never guess.
_OTHER_DAY = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thurs?|fri|sat|sun)\b"
    r"|\bnext\s+(?:week|weekend|month|year)\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}\b|\b\d{4}-\d{2}-\d{2}\b"
    r"|(?<!\w)[+-]\d{2}:?\d{2}(?!\w)"
    r"|\b(?:yesterday|weekend|day\s+after|in\s+\w+\s+(?:days?|weeks?|months?)|"
    r"(?:EST|EDT|CST|CDT|MST|MDT|PST|PDT|UTC|GMT)|"
    r"(?:eastern|central|mountain|pacific|local|your|my)\s+time|timezone)\b", re.I)
_HEDGE = re.compile(
    r"\?|\b(?:maybe|perhaps|probably|possibly|might|could\s+we|can\s+we|should\s+we|"
    r"if\s+(?:you|we|i|it)|tentative|tbd|tbc|or\s+so|around|about|approx\w*|roughly|"
    r"cancel\w*|resched\w*|moved|postponed|not|never|won['’]?t|cannot|can['’]?t|"
    r"don['’]?t|would|could|should|unless|hopefully|hoping|hope|try|trying|"
    r"depending|assuming|aiming)\b", re.I)
# What makes a time a plan rather than a passing mention.
_PLAN = re.compile(
    r"\b(?:meet|meeting|see\s+you|pick\s*(?:you\s*)?up|drop\s*(?:you\s*)?off|dinner|lunch|"
    r"breakfast|brunch|coffee|drinks|class|lecture|exam|quiz|interview|appointment|flight|"
    r"reservation|come\s+over|come\s+by|call\s+me|game|practice|rehearsal|party|"
    r"be\s+there|be\s+at|show\s+up|leaving|leave|there|at\s+the)\b", re.I)
_PROMO = re.compile(
    r"https?://|www\.|\b\d{1,3}\s?%\s*off\b|\breply\s+stop\b|\bopt\s*out\b|\bunsubscribe\b|"
    r"\bfree\b|\bwinner\b|\bcongratulations\b|\bact\s+now\b|\blimited\s+time\b|\bverify\b|"
    r"\bclick\b|\bgift\s+card\b|\bclaim\b|\bpromo\w*|\bcoupon\b|\bsale\b|\bdiscount\b|"
    r"\byour\s+(?:package|account|order|parcel)\b|\bdelivery\b|\bpassword\b|\bone[- ]time\s+code\b|"
    r"\bverification\s+code\b", re.I)
_STOP = {"the", "and", "with", "for", "you", "your", "that", "this", "have", "will", "from",
         "meet", "call", "see", "at", "in", "me", "to", "be", "a", "an", "of", "on", "pm", "am"}

# Apply full-source exclusions to rules and local extraction alike. A positive
# clause cannot strip a condition, quotation, cancellation or instruction.
_UNSAFE_CONTEXT = re.compile(
    r"\b(?:if|unless|pretend|imagine|hypothetical|example|quoted?|forwarded|"
    r"no\s+longer|used\s+to|ignore|instructions?|system|assistant)\b|[\"“”`>]", re.I)


@dataclass(frozen=True)
class Candidate:
    reason: str
    item_id: str
    sender: str
    conversation: str
    when_ts: float
    quote: str          # verbatim substring of the message text
    text: str
    rule: str           # which rule fired, for the "why" line


def is_promo_or_scam(sender: str, text: str) -> bool:
    s = (sender or "").strip()
    if _PROMO.search(text or ""):
        return True
    # Short codes and alphanumeric sender IDs ("SHOP", "48921") are businesses, not people.
    if re.fullmatch(r"\+?\d{3,7}", s):
        return True
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9\-]{2,10}", s)) and not s.isdigit() and s == s.upper()


def _clock(text: str) -> tuple[int, int, re.Match] | None:
    """The single unambiguous clock time in ``text``; None if none or competing."""
    found: list[tuple[int, int, re.Match]] = []
    for m in _AMPM.finditer(text):
        hour = int(m["h"]) % 12 + (12 if m["ap"].lower() == "p" else 0)
        found.append((hour, int(m["m"] or 0), m))
    for m in _H24.finditer(text):
        found.append((int(m["h"]), int(m["m"]), m))
    for m in _NOON.finditer(text):
        found.append((12, 0, m))
    if len({(h, mi) for h, mi, _ in found}) != 1:
        return None
    return found[0]


def _sentence(text: str, match: re.Match) -> str:
    """The sentence holding the time, as an exact substring of ``text``."""
    start = max((text.rfind(c, 0, match.start()) for c in ".!?\n"), default=-1) + 1
    # "6 p.m." swallows its closing period into the match; that period still ends the sentence.
    after = match.end() - 1 if match[0].endswith(".") else match.end()
    ends = [i for i in (text.find(c, after) for c in ".!?\n") if i != -1]
    end = min(ends) if ends else len(text)
    return text[start:end].strip()


def resolve_when(text: str, sent_at: float) -> tuple[float, re.Match] | None:
    """Resolve the stated time to an epoch, or None when it is not unambiguous."""
    if _HEDGE.search(text) or _OTHER_DAY.search(text) or _UNSAFE_CONTEXT.search(text):
        return None
    clock = _clock(text)
    if clock is None:
        return None
    hour, minute, match = clock
    if _TOMORROW.search(text) and _TODAY.search(text):
        return None
    base = datetime.fromtimestamp(sent_at)
    day = base.date() + timedelta(days=1 if _TOMORROW.search(text) else 0)
    when = datetime(day.year, day.month, day.day, hour, minute)
    if not _TOMORROW.search(text) and not _TODAY.search(text) and when <= base:
        when += timedelta(days=1)       # no day word: the next time it is that time
    elif when <= base:
        return None                     # "today at 9am" sent at 10am is not a future plan
    # Local wall times during the DST gap/fold do not identify one instant.
    if when.timestamp() != when.replace(fold=1).timestamp():
        return None
    return when.timestamp(), match


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP}


def already_on_file(when_ts: float, text: str, commitments: list[dict]) -> bool:
    mine = _words(text)
    for c in commitments:
        w = c.get("when_ts")
        if w is None or c.get("status", "active") != "active":
            continue
        gap = abs(float(w) - when_ts)
        if gap <= KNOWN_NEAR_S or (gap <= KNOWN_RELATED_S and mine & _words(c.get("title", ""))):
            return True
    return False


def detect(items: list[Item], *, now: float, commitments: list[dict],
           horizon_s: float = HORIZON_S) -> list[Candidate]:
    """Candidates for ``items`` (oldest first). ``commitments`` is what is on file."""
    out: list[Candidate] = []
    seen: list[tuple[float, str]] = []
    for item in sorted(items, key=lambda i: i.ts):
        text = item.text or ""
        if item.direction != "incoming" or not text.strip() or not item.sender.strip():
            continue
        if is_promo_or_scam(item.sender, text) or not _PLAN.search(text):
            continue
        resolved = resolve_when(text, item.ts)
        if resolved is None:
            continue
        when_ts, match = resolved
        if not (now <= when_ts <= now + horizon_s):
            continue
        quote = _sentence(text, match)
        if not quote or quote not in text:
            continue                    # grounded or absent
        if already_on_file(when_ts, text, commitments):
            continue
        # The same plan restated in a thread is one alert.
        if any(abs(when_ts - w) < 60 and item.conversation == c for w, c in seen):
            continue
        seen.append((when_ts, item.conversation))
        out.append(Candidate(REASON, item.id, item.sender, item.conversation, when_ts, quote, text,
                             "stated a near-term plan with no matching item in Wisp's synced schedule"))
    return out
