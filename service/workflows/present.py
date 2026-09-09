"""Turn quoted source text into text a RECIPIENT can read.

`execute_workflow` never lets a model author an outbound payload, so the words
that reach a recipient are the words a tool produced. Those strings are written
for a model, and on 2026-09-08 a "send dad my schedule for the next 10 days"
delivery put all of it in the message:

    Wisp report from the sender's connected sources. Any 'you/your' in the
    excerpts refers to the sender.

    Calendar — source excerpt:
    Today is Tuesday, September 8, 2026. Upcoming (next 10 day(s), 17 item(s))
    — each row is tagged relative to today:
    Calendar events: 8; Wisp/Apple reminders: 9. A calendar event alone is not
    a reminder.
    - Thu Sep 17 (Thu Sep 17) 8:15 AM (in 8 d) meeting: Move-in [Adi Jain] …

Four of those lines only ever addressed the model — they exist so a small model
cannot mis-date a row or read an event as a reminder — and every far-out row
printed its date twice, because `_day_tag` falls back to the same "%a %b %-d"
the parenthetical already carries. The user reported it as leaked reasoning and
repeated events; both are this text, not the model's.

This pass is deliberately NOT a model. It drops model-facing scaffolding, and
re-lays rows it has already parsed — the "[Apple Reminder]" tag each row carried
becomes the group it sits under, which is the same move `brief._schedule_section`
made for the on-screen summary. It never adds, infers or rewords a fact, and it
never drops one: a row it cannot parse makes it hand the whole source back
sanitized-only, so an unrecognised format degrades to the old behaviour instead
of losing content.
"""
from __future__ import annotations

import re

# One `_fmt` row from service.tools.assistant_tools. The parenthetical date is
# optional: `_day_tag` omits it once the tag IS the date (a week or more out).
_ROW = re.compile(
    r"^- (?P<day>TODAY|TOMORROW|[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2}|[A-Z][a-z]+)"
    r"(?: \((?P<date>[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2})\))?"
    r" (?P<clock>all day|\d{1,2}:\d{2} [AP]M)"
    r" \((?:now|in [^)]+)\)"
    r" (?P<kind>[A-Za-z]+): (?P<rest>.+)$")

_DATE_ONLY = re.compile(r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2}$")

_ORIGIN = r"Calendar event|Apple Reminder|Wisp reminder; Apple mirror not verified"
_TAG = re.compile(rf"\s*\[(?P<origins>(?:{_ORIGIN})(?:\s*/\s*(?:{_ORIGIN}))*)\]$")

_UPCOMING_HEADER = re.compile(
    r"^Today is .+?\. Upcoming \((?P<window>[^,]+), \d+ item\(s\)\) — "
    r"each row is tagged relative to today:$")
# The counts exist only to set up "A calendar event alone is not a reminder";
# the group headings below say the same thing structurally.
_COUNTS = re.compile(r"^Calendar events: \d+; Wisp/Apple reminders: \d+\.")

# Sentences a source appends to an otherwise readable line purely to brief the
# model on how to read it. Removed in place; the line itself survives.
_SCAFFOLD_SENTENCE = re.compile(
    r"\s*(?:A calendar event alone is not a reminder\.|"
    r"Quoted from the source; the names, accounts and dates are the sender's "
    r"own, not conclusions\.|"
    r"Source excerpts \(not inferred outcomes\);[^\n]*?original messages\.)")

_SECOND_PERSON = re.compile(r"\b(?:you|your|yours|you're|my|mine)\b", re.I)


def sender_name() -> str:
    """The sender's own name, or "" when it can't be determined."""
    try:
        from service.memory.identity import user_name
        return user_name().strip()
    except Exception:  # noqa: BLE001 — attribution is best-effort, never fatal
        return ""


def _sanitize(raw: str) -> str:
    kept = []
    for line in raw.splitlines():
        if _UPCOMING_HEADER.match(line) or _COUNTS.match(line):
            continue
        line = _SCAFFOLD_SENTENCE.sub("", line).rstrip()
        if line or kept:
            kept.append(line)
    return "\n".join(kept).strip()


def _window(raw: str) -> str:
    for line in raw.splitlines():
        header = _UPCOMING_HEADER.match(line)
        if header:
            window = header.group("window").replace(" day(s)", " days")
            return re.sub(r"\bnext 1 days\b", "tomorrow", window)
    return ""


def _row(line: str) -> tuple[str, str] | None:
    """One rendered row plus the group it belongs in, or None if unparsable."""
    match = _ROW.match(line)
    if not match:
        return None
    rest = match.group("rest")
    tag = _TAG.search(rest)
    origins = tag.group("origins") if tag else ""
    rest = _TAG.sub("", rest).strip()
    day, date, kind = match.group("day"), match.group("date"), match.group("kind")
    when = date or (day if _DATE_ONLY.match(day) else day)
    if day == "TODAY":
        when += " (today)"
    elif day == "TOMORROW":
        when += " (tomorrow)"
    # "meeting"/"event"/"reminder" is what the group heading already says; a
    # kind the heading does not cover ("EXAM", "due") is a fact and stays.
    label = "" if kind.lower() in {"meeting", "event", "reminder"} else f"{kind}: "
    caveat = (" (added in Wisp; not verified in Apple Reminders)"
              if "Apple mirror not verified" in origins else "")
    clock = "all day" if match.group("clock") == "all day" else match.group("clock")
    group = ("Reminders" if ("Apple Reminder" in origins or "Wisp reminder" in origins
                             or (not origins and kind.lower() == "reminder"))
             else "Events")
    return group, f"- {when}, {clock} — {label}{rest}{caveat}"


def _calendar(raw: str) -> str | None:
    """get_upcoming output as grouped, recipient-facing rows.

    None when any row fails to parse — the caller then falls back to the
    sanitized source rather than shipping a partially understood schedule.
    """
    lead, groups = [], {"Events": [], "Reminders": []}
    for line in raw.splitlines():
        if not line.strip() or _UPCOMING_HEADER.match(line) or _COUNTS.match(line):
            continue
        if not line.startswith("- "):
            lead.append(_SCAFFOLD_SENTENCE.sub("", line).rstrip())
            continue
        row = _row(line)
        if row is None:
            return None
        group, text = row
        if text not in groups[group]:     # an identical row twice is one row
            groups[group].append(text)
    if not any(groups.values()):
        return None
    window = _window(raw)
    name = sender_name()
    heading = f"{name}’s schedule" if name else "Schedule"
    blocks = [heading + (f" — {window}" if window else "")]
    blocks += [line for line in lead if line]
    for group, rows in groups.items():
        if rows:
            blocks.append(f"{group}\n" + "\n".join(rows))
    return "\n\n".join(blocks)


def render(source: str, raw: str) -> tuple[str, bool]:
    """One source as recipient-facing text, plus whether it is self-attributed.

    Self-attributed means the block names whose data it is, so the payload does
    not need the "any 'you/your' refers to the sender" preamble on top of it.
    """
    if source == "calendar":
        rendered = _calendar(raw)
        if rendered is not None:
            return rendered, bool(sender_name())
    body = _sanitize(raw)
    heading = source.replace("_", " ").title()
    return f"{heading}:\n{body}", not _SECOND_PERSON.search(body)


def compose(sections: list[tuple[str, str]]) -> str:
    """The outbound payload for a list of (source, raw tool output) pairs."""
    blocks, attributed = [], True
    for source, raw in sections:
        text, self_attributed = render(source, raw)
        blocks.append(text)
        attributed = attributed and self_attributed
    body = "\n\n".join(blocks)
    if attributed:
        return body
    name = sender_name()
    who = f"{name}’s" if name else "the sender’s"
    return (f"Shared from {who} Wisp. Any ‘you/your’ below refers to "
            f"{name or 'the sender'}.\n\n" + body)
