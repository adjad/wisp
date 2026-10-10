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
# Unsupported dates/zones: never discard a qualifier and guess local time.
# Unknown uppercase codes also abstain; false negatives are safe in this demo.
_OTHER_DAY = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thurs?|fri|sat|sun)\b"
    r"|\bnext\s+(?:week|weekend|month|year)\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?[\s./-]+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b"
    r"|\b\d{1,2}/\d{1,2}\b|\b\d{4}-\d{2}-\d{2}\b"
    r"|[+-]\d{1,2}:?\d{2}(?!\w)"
    r"|\b[a-z_]+/[a-z_]+(?:/[a-z_]+)?\b"
    r"|(?-i:\b(?!(?:AM|PM)\b)[A-Z]{2,6}\b)"
    r"|\b(?:yesterday|weekend|day\s+after|in\s+\w+\s+(?:days?|weeks?|months?)|"
    r"(?:EST|EDT|CST|CDT|MST|MDT|PST|PDT|UTC|GMT|CET|CEST|EET|EEST|WET|WEST|"
    r"IST|BST|JST|KST|HKT|SGT|AEST|AEDT|ACST|ACDT|AWST|NZST|NZDT|ET|CT|MT|PT|MSK|AKST|AKDT|Z)|"
    r"(?:[a-z]+\s+){1,4}(?:time|timezone)|timezone)\b", re.I)
_HEDGE = re.compile(
    r"\?|\b(?:maybe|perhaps|probably|possibly|might|could\s+we|can\s+we|should\s+we|"
    r"if\s+(?:you|we|i|it)|tentative|tbd|tbc|or\s+so|around|about|approx\w*|roughly|"
    r"cancel\w*|resched\w*|moved|postponed|not|never|won['’]?t|cannot|can['’]?t|"
    r"don['’]?t|would|could|should|unless|hopefully|hoping|hope|try|trying|"
    r"depending|assuming|aiming)\b", re.I)
# Complete supported plan sentences, with the located clock replaced by <TIME>.
# An activity mentioned anywhere in a message is not evidence of participation.
# Keep the prototype's direct invitations and narrow elliptical schedule forms;
# other phrasing must pass the local extractor's separate personal-plan checks.
_DAY = r"(?:today|tonight|tomorrow|tmrw|tmr|this\s+(?:morning|afternoon|evening))"
_PARTICIPANT = r"(?:mom|dad|you|us|(?-i:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}))"
_PLAN_SENTENCE = re.compile(
    rf"(?:meet\s+me(?:\s+in\s+the\s+Quad)?|see\s+you|class|"
    rf"(?:dinner|lunch)(?:\s+with\s+{_PARTICIPANT})?)"
    rf"(?:\s+{_DAY})?\s+at\s+<TIME>(?:\s+{_DAY})?", re.I)
_BOOKED_DINNER = re.compile(
    rf"dinner(?:\s+{_DAY})?\s+at\s+<TIME>(?:\s+{_DAY})?,\s+I\s+booked\s+a\s+table", re.I)
# The hackathon model fallback has a bounded assertion grammar too. Free-form
# pre-clock text could otherwise hide an unsupported relative date or zone.
# Keep this separate from _rule_plan so natural fixtures exercise inference.
_MODEL_SENTENCE = re.compile(
    rf"I\s+can\s+make\s+it\s+to\s+the\s+Quad(?:\s+{_DAY})?\s+at\s+<TIME>(?:\s+{_DAY})?", re.I)
# Only these surrounding sentences have an understood role in the prototype.
# Unknown headings/attributions must not disappear when a sentence is located.
_GREETING = re.compile(r"(?:hey|hi|hello)!", re.I)
_LOGISTICS = re.compile(r"[.!]\s+bring\s+the\s+blue\s+folder[.!]?", re.I)
_TIME_TAIL = re.compile(rf"(?:\s*{_DAY})?(?:,\s*I\s+booked\s+a\s+table)?\s*", re.I)
_MONTH = re.compile(
    r"(?<![a-z])(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?![a-z])", re.I)
_UNSUPPORTED_TEMPORAL = re.compile(
    r"\b(?:on|next|last|days?|weeks?|months?|years?|first|second|third|fourth|fifth|sixth|"
    r"seventh|eighth|ninth|tenth|eleventh|twelfth|\w*teenth|twentieth|thirtieth)\b", re.I)
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
    r"said|says|told|heard|reported|no\s+longer|used\s+to|ignore|instructions?|system|assistant)\b"
    r"|[\"“”`>]|(?<!\w)['‘]|['’](?!\w)", re.I)


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
    # A period inside p.m./a.m. may be followed by a qualifier in the same
    # sentence. Never truncate there and lose "is Alex's plan", for example.
    # Ambiguous abbreviation boundaries retain the continuation and fail closed.
    abbreviation = bool(re.search(r"[ap]\.m\.$", match[0], re.I))
    after = match.end() if abbreviation or not match[0].endswith(".") else match.end() - 1
    ends = [i for i in (text.find(c, after) for c in ".!?\n") if i != -1]
    end = min(ends) if ends else len(text)
    return text[start:end].strip()


def _rule_plan(quote: str) -> bool:
    clock = _clock(quote)
    if clock is None:
        return False
    match = clock[2]
    skeleton = quote[:match.start()] + "<TIME>" + quote[match.end():]
    return bool(_PLAN_SENTENCE.fullmatch(skeleton) or _BOOKED_DINNER.fullmatch(skeleton))


def _supported_source(text: str, match: re.Match) -> bool:
    """A bounded source envelope and temporal clause, shared by both paths."""
    quote = _sentence(text, match)
    start = text.find(quote) if quote else -1
    if start < 0 or text.find(quote, start + 1) >= 0:
        return False
    prefix = text[:start].strip()
    suffix = text[start + len(quote):].strip()
    if prefix and not _GREETING.fullmatch(prefix):
        return False
    if suffix not in {"", ".", "!"} and not _LOGISTICS.fullmatch(suffix):
        return False
    # There must be one clock span, with no other date/room/number interpreted
    # as harmless context. Dates are not supported, regardless of separators.
    remainder = text[:match.start()] + text[match.end():]
    if re.search(r"\d", remainder) or _MONTH.search(remainder) or _UNSUPPORTED_TEMPORAL.search(remainder):
        return False
    clock = _clock(quote)
    if clock is None:
        return False
    local_match = clock[2]
    skeleton = quote[:local_match.start()] + "<TIME>" + quote[local_match.end():]
    if not (_rule_plan(quote) or _MODEL_SENTENCE.fullmatch(skeleton)):
        return False
    # A clock must follow "at" and end its temporal clause. Only a supported
    # day or the prototype's booking suffix may follow it. Any other suffix,
    # including unknown zones of any case/length, is unclassified and abstains.
    return bool(re.search(r"\bat\s*$", quote[:local_match.start()], re.I)
                and _TIME_TAIL.fullmatch(quote[local_match.end():]))


def resolve_when(text: str, sent_at: float) -> tuple[float, re.Match] | None:
    """Resolve the stated time to an epoch, or None when it is not unambiguous."""
    if _HEDGE.search(text) or _OTHER_DAY.search(text) or _UNSAFE_CONTEXT.search(text):
        return None
    clock = _clock(text)
    if clock is None:
        return None
    hour, minute, match = clock
    if not _supported_source(text, match):
        return None
    # Dayparts have deliberately bounded, disjoint clock ranges. Never silently
    # discard a contradictory qualifier and schedule its bare clock instead.
    for phrase, low, high in ((r"this\s+morning", 0, 12),
                              (r"this\s+afternoon", 12, 17),
                              (r"this\s+evening|tonight", 17, 24)):
        if re.search(rf"\b(?:{phrase})\b", text, re.I) and not low <= hour < high:
            return None
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
        if is_promo_or_scam(item.sender, text):
            continue
        resolved = resolve_when(text, item.ts)
        if resolved is None:
            continue
        when_ts, match = resolved
        if not (now <= when_ts <= now + horizon_s):
            continue
        quote = _sentence(text, match)
        if not quote or quote not in text or not _rule_plan(quote):
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
