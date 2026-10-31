"""A cheap, deterministic grounding check on the final Ask answer.

Toolbench 2026-10-31 (Gemma, baseline 25c3a289): 175 ungrounded answers and
most no_call failures. Two shapes this can see without another model call:

- A specific value in the answer that nothing this turn saw: a clock time,
  a calendar date, a money amount, an email address, a quoted title or a
  person's name after "with/from/to/by". Checked only when a tool returned
  results this turn: a general-knowledge answer, a calculation or plain chat
  has no results to check against and always passes.
- A claim that something was done or checked ("I've set a timer", "I've
  checked, and…") or a promise to do it ("I will start by running…") when no
  tool ran this turn, on a request for an action or for the user's own data.

On a hit the loop re-prompts the model ONCE with what did not match, then
accepts whatever comes back. This module never edits the answer, and a clean
answer costs nothing beyond these regexes.

Avoiding false positives:
- The evidence is everything the model was shown this turn: the system
  prompt (today's date, the user's identity), the history, the user's words,
  every tool result and the model's own call arguments (a drafted text).
- A date within a week either side of today passes: "tomorrow, Sunday,
  November 1" restates a relative date, and an exact weekday date is what a
  "this Friday" answer should say.
- A money amount passes when it is the sum or difference of two amounts the
  results show (a total, a change).
- A bare "3:30" in the results counts as both 3:30 AM and 3:30 PM.
- The user's own name and product names are never a mismatched "person".
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Iterable

MAX_LISTED = 4

_MONTHS = {m: i for i, names in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
    ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
    ("oct", "october"), ("nov", "november"), ("dec", "december")), 1) for m in names}
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_MD = re.compile(_MONTH + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_DATE_DM = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH + r"\b", re.I)
_DATE_ISO = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})")
_DATE_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b")

_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?", re.I)
_AT_HOUR = re.compile(r"\b(?:at|by|around|until|till)\s+(\d{1,2})\b(?![:\d])", re.I)
_CLOCK_24 = re.compile(r"(?:\b|T)(\d{1,2}):(\d{2})(?::\d{2})?\b")

_MONEY = re.compile(r"[$€£]\s?(\d[\d,]*(?:\.\d{1,2})?)|\b(\d[\d,]*\.\d{2})\s?(?:usd|dollars|eur|euros|gbp)\b", re.I)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_QUOTED = re.compile(r"[\"“]([^\"”\n]{3,80})[\"”]")
_PERSON = re.compile(r"\b(?:with|from|to|by|for|and)\s+(?:Dr\.?\s+)?([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)")
_NOT_PEOPLE = frozenset("""
monday tuesday wednesday thursday friday saturday sunday today tomorrow tonight yesterday
january february march april may june july august september october november december
wisp gmail google apple icloud imessage messages mail calendar reminders notes contacts finder
outlook slack zoom teams safari chrome siri spotify music photos maps weather the this that
your you everyone everybody anyone someone nothing
""".split())

_DONE_CLAIM = re.compile(
    r"\bi(?:'|’)ve\s+(?:just\s+|now\s+|already\s+|gone\s+ahead\s+and\s+)?(?:sent|added|created|scheduled|set|saved|"
    r"deleted|removed|moved|cancel+ed|texted|emailed|messaged|replied|forwarded|booked|updated|"
    r"completed|marked|made\s+sure|organi[sz]ed|sorted|cleared|checked|looked|confirmed|verified|"
    r"reviewed|pulled|found)\b"
    r"|\bi\s+have\s+(?:just\s+|now\s+|already\s+)?(?:sent|added|created|scheduled|set|saved|deleted|"
    r"removed|moved|cancel+ed|updated|checked|confirmed|verified)\b"
    r"|\bi\s+can\s+confirm\b", re.I)
_PROMISE = re.compile(
    r"\bi(?:'|’)ll\s+(?:now\s+|go\s+ahead\s+and\s+)?(?:start|run|check|look|pull|search|set|send|move|"
    r"sort|organi[sz]e|clear|cancel|delete|add|create|update|get\s+(?:that|this|it))\b"
    r"|\bi\s+will\s+(?:now\s+|go\s+ahead\s+and\s+)?(?:start|run|check|look|pull|search|set|send|move|"
    r"sort|organi[sz]e|clear|cancel|delete|add|create|update)\b"
    r"|\b(?:let\s+me|i(?:'|’)m\s+(?:going\s+to|about\s+to))\s+(?:check|look|pull|search|run|start|get)\b",
    re.I)


def _norm(text: str) -> str:
    return " ".join((text or "").casefold().split())


def _month(name: str) -> int:
    key = name.casefold().rstrip(".")
    return _MONTHS.get(key) or _MONTHS.get(key[:3]) or 0


def _dates(text: str, year: int) -> set[tuple[int, int]]:
    out = set()
    for m in _DATE_MD.finditer(text):
        out.add((_month(m.group(1)), int(m.group(2))))
    for m in _DATE_DM.finditer(text):
        out.add((_month(m.group(2)), int(m.group(1))))
    return {d for d in out if d[0] and 1 <= d[1] <= 31}


def _evidence_dates(text: str, year: int) -> set[tuple[int, int]]:
    out = _dates(text, year)
    out |= {(int(m.group(2)), int(m.group(3))) for m in _DATE_ISO.finditer(text)}
    out |= {(int(m.group(1)), int(m.group(2))) for m in _DATE_SLASH.finditer(text)
            if 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31}
    return out


def _clock(m) -> int:
    hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
    return (hour + (12 if m.group(3).casefold() == "p" else 0)) * 60 + minute


def _evidence_clocks(text: str) -> set[int]:
    out = {_clock(m) for m in _CLOCK.finditer(text) if int(m.group(1)) <= 12}
    for m in _AT_HOUR.finditer(text):
        hour = int(m.group(1))
        if 1 <= hour <= 12:
            out |= {(hour % 12) * 60, (hour % 12 + 12) * 60}
    if re.search(r"\bnoon\b", text, re.I):
        out.add(12 * 60)
    if re.search(r"\bmidnight\b", text, re.I):
        out.add(0)
    for m in _CLOCK_24.finditer(text):
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour <= 23 and minute <= 59:
            out.add(hour * 60 + minute)
            if hour <= 12:
                out.add((hour % 12) * 60 + minute)
                out.add((hour % 12 + 12) * 60 + minute)
    return out


def _amount(raw: str) -> float | None:
    try:
        return round(float(raw.replace(",", "")), 2)
    except ValueError:
        return None


def _evidence_numbers(text: str) -> set[float]:
    return {a for a in (_amount(m.group(0)) for m in _NUMBER.finditer(text)) if a is not None}


def _near_today(month: int, day: int, today: _dt.date) -> bool:
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            when = _dt.date(year, month, day)
        except ValueError:
            continue
        if abs((when - today).days) <= 7:
            return True
    return False


def unmatched_values(answer: str, evidence: str, *, today: _dt.date,
                     own_names: Iterable[str] = ()) -> list[str]:
    """Specific values in the answer that nothing in the evidence shows."""
    found: list[str] = []
    low = _norm(evidence)

    clocks = _evidence_clocks(evidence)
    for m in _CLOCK.finditer(answer):
        if int(m.group(1)) <= 12 and _clock(m) not in clocks:
            found.append(m.group(0).strip())

    dates = _evidence_dates(evidence, today.year)
    for m in list(_DATE_MD.finditer(answer)) + list(_DATE_DM.finditer(answer)):
        for month, day in _dates(m.group(0), today.year):
            if (month, day) not in dates and not _near_today(month, day, today):
                found.append(m.group(0).strip())

    numbers = _evidence_numbers(evidence)
    amounts = sorted(numbers)[:60]
    for m in _MONEY.finditer(answer):
        value = _amount(m.group(1) or m.group(2))
        if value is None or value in numbers:
            continue
        if any(abs(a + b - value) < 0.006 or abs(abs(a - b) - value) < 0.006
               for i, a in enumerate(amounts) for b in amounts[i:]):
            continue
        found.append(m.group(0).strip())

    for m in _EMAIL.finditer(answer):
        if m.group(0).casefold() not in low:
            found.append(m.group(0))

    for m in _QUOTED.finditer(answer):
        quoted = _norm(m.group(1)).strip(" .,!?*")
        if len(quoted.split()) >= 2 and quoted not in low:
            found.append(f'"{m.group(1).strip()}"')

    own = {_norm(n) for n in own_names if n}
    for m in _PERSON.finditer(answer):
        name = m.group(1)
        parts = [p for p in _norm(name).split() if p not in _NOT_PEOPLE]
        if not parts or _norm(name) in own or any(p in own for p in parts):
            continue
        if not all(p in low for p in parts):
            found.append(name)

    return list(dict.fromkeys(found))[:MAX_LISTED]


def unbacked_claim(answer: str) -> str:
    """The "I've set it" / "I've checked" / "I'll start by…" phrase, or ""."""
    text = answer or ""
    if m := _DONE_CLAIM.search(text):
        return m.group(0)
    if "?" not in text and (m := _PROMISE.search(text)):
        return m.group(0)
    return ""


def evidence_text(msgs: list[dict]) -> str:
    """Everything the model was shown this turn, plus its own call arguments."""
    parts = []
    for msg in msgs or ():
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts += [str(p.get("text") or "") for p in content if isinstance(p, dict)]
        for call in msg.get("tool_calls") or ():
            fn = (call or {}).get("function") or {}
            args = fn.get("arguments")
            parts.append(args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False))
    return "\n".join(parts)


def check(answer: str, msgs: list[dict], *, ran_tools: bool, had_results: bool,
          asks_action: bool, asks_personal: bool, today: _dt.date | None = None,
          own_names: Iterable[str] = ()) -> str:
    """The one-time correction to send the model, or "" when the answer is clean."""
    answer = answer or ""
    if not answer.strip():
        return ""
    if not ran_tools and (asks_action or asks_personal):
        claim = unbacked_claim(answer)
        if claim:
            return ("Check your reply: it says \"" + claim.strip() + "\", but no tool has run on this "
                    "turn, so nothing was checked, set, saved, sent or changed. If this request "
                    "needs a tool, call it now; otherwise answer without claiming you did it.")
    if had_results:
        today = today or _dt.date.today()
        values = unmatched_values(answer, evidence_text(msgs), today=today, own_names=own_names)
        if values:
            return ("Check your reply against this turn's tool results: "
                    + ", ".join(values) + (" does" if len(values) == 1 else " do")
                    + " not appear in them. Correct anything the results do not support (do not "
                    "guess), keep a value only if it follows from the results, and reply again.")
    return ""
