"""Bounded Messages digests, with source-owned attribution and offline fallback.

The model may rank topic candidates, never author claims, identities, or reply
status. Rendering is deterministic even on the successful model path. This
prevents a source echo (or an invented acceptance) becoming a presynthesized
tool answer. Raw bodies are used only for analysis and diagnostic capture.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

MAX_OUTPUT_CHARS = 4800
MAX_CONVERSATIONS = 10
MAX_BODY_CHARS = 4000
MAX_TOPICS = 3

# These labels describe subjects, not conclusions about the user's life.
_TOPICS = {
    "meals": r"\b(?:dinner|lunch|breakfast|restaurant|food|pizza|brunch)\b",
    "meetups": r"\b(?:meet|meeting|meetup|party|hang|gather|drinks)\b",
    "travel": r"\b(?:flight|train|airport|trip|travel|hotel|ride|pickup)\b",
    "work": r"\b(?:project|report|review|document|proposal|client|deadline|slides)\b",
    "school": r"\b(?:class|course|exam|homework|lecture|campus)\b",
    "family": r"\b(?:family|birthday|anniversary|wedding)\b",
    "health": r"\b(?:doctor|appointment|hospital|sick|medicine)\b",
    "payments": r"\b(?:pay|payment|money|rent|invoice|venmo|cost)\b",
}
_TIME = re.compile(
    r"\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2}|"
    r"today|tomorrow|tonight|yesterday|(?:this|next|last)\s+(?:week|month|weekend)|"
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)|"
    r"\d{4}-\d{2}-\d{2}|in\s+\d+\s+(?:minutes?|hours?|days?))\b", re.I)
_DECISION = re.compile(r"\b(?:agreed|decided|confirmed|chose|selected|cancelled|canceled|postponed)\b", re.I)
_ACTION = re.compile(r"\b(?:please|can you|could you|would you|need you to|remember to|do not|don't|i(?:'|’)ll|i will)\b", re.I)
_QUESTION = re.compile(r"\?|\b(?:let me know|rsvp|please (?:reply|respond)|can you|could you|would you)\b", re.I)
_PLAN = re.compile(r"\b(?:let(?:'|’)s|plan|planning|meet|dinner|lunch|appointment|flight|schedule)\b", re.I)
_REACTION = re.compile(
    r"^(?:(?:liked|loved|laughed at|emphasized|questioned|disliked)\s+[\"“].*|"
    r"(?:ok(?:ay)?|yes|no|thanks|thank you|lol|haha|nice|cool|great|yep|yup|👍|❤️|😂)[.!\s]*)$", re.I)
_WORDS = re.compile(r"[\w’'-]+")
_OBJECT = re.compile(
    r"\b(?:send|bring|review|finish|submit|book|choose|chose|selected|pay|confirm|cancel|"
    r"share|buy|pick up|decided on|agreed on)\s+(?:the\s+|a\s+|an\s+)?([^.!?;,\n]{2,70})", re.I)
_NEGATIVE = re.compile(r"\b(?:not|never|no longer|cannot|can't|won't|don't|didn't|isn't|hasn't|haven't)\b|n['’]t\b", re.I)
_CONDITIONAL = re.compile(r"\b(?:if|unless|whether|may|might|maybe|possibly|pending|should|could|would|perhaps|hope|suggest|propose)\b|\blet['’]s\b", re.I)
_STATUS = re.compile(
    r"\b(?:cancel(?:l)?ed|postponed|rescheduled|delayed|confirmed|increased|decreased|"
    r"raised|reduced|moved|happening|on time|is off|are off|called off|back on)\b", re.I)
_CORRECTION = re.compile(r"^(?:actually|correction|update|instead)\b", re.I)
_MOVED = re.compile(r"\b(?:rescheduled|postponed|delayed|moved)\b", re.I)
_ENTITY = re.compile(r"\b(?:dinner|lunch|breakfast|brunch|flight|appointment|meeting|rent|invoice|payment|order)\b", re.I)
_IDENTIFIER = re.compile(
    r"\s*(?:#\s*|number\s+)?((?!(?:at|on|in|by|to|for)\s)[a-z]{0,3}\s*\d+(?:[-/]\w+)*)\b", re.I)
_AMOUNT = re.compile(r"(?:[$£€]\s*\d[\d,.]*|\b\d[\d,.]*\s*(?:dollars?|euros?|pounds?|percent|%))", re.I)
_STOP = set("a an and are as at be been but by can could did do does for from had has have "
            "he her here him his how i if in is it its just me my of on or our she so some "
            "that the their them then there these they this to too us was we were what when "
            "where which who why will with would you your hi hey hello please thanks today "
            "tomorrow tonight yesterday am pm".split())


def plain(value: str, limit: int = 80, *, quoted: bool = False) -> str:
    """Names/labels are data too: never allow Markdown or multiline framing."""
    value = re.sub(r"[\x00-\x1f\x7f*`\[\]<>|\\]", " ", value)
    if not quoted:
        value = value.replace("#", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


class SummaryRow(tuple):
    """A normal three-field source row with text-free context provenance.

    Row metadata survives sorting and recent-message selection without changing
    tuple unpacking, equality, counts, diagnostic rendering, or model input.
    """
    def __new__(cls, row, before: int, after: int):
        value = super().__new__(cls, row)
        value.source_before = before
        value.source_after = after
        return value


def with_source_positions(rows: list[tuple[float, str, str]]) -> list[SummaryRow]:
    """Count substantive source rows before filtering, in each conversation."""
    if all(isinstance(row, SummaryRow) for row in rows):
        return list(rows)  # repeated filtering must not erase earlier gaps
    positions: Counter = Counter()
    annotated = {}
    for index in sorted(range(len(rows)), key=lambda i: rows[i][0]):
        row = rows[index]
        before = positions[row[1]]
        body = split_sender(row[2])[1]
        if not _REACTION.fullmatch(body) and _WORDS.search(body):
            positions[row[1]] += 1
        annotated[index] = SummaryRow(row, before, positions[row[1]])
    return [annotated[index] for index in range(len(rows))]


@dataclass
class Conversation:
    label: str
    count: int = 0
    reactions: int = 0
    truncated: bool = False
    topics: Counter = field(default_factory=Counter)
    # Each category aggregates sender/recipient and subject, not source rows.
    signals: dict[str, list[str]] = field(default_factory=lambda: {
        "Decisions mentioned": [], "Plans / times mentioned": [],
        "Action items mentioned": [], "Reply check": [], "Updates": [],
    })
    states: list["StateReport"] = field(default_factory=list)
    # Only an established report at the preceding substantive clause can be
    # an implicit antecedent. Untracked source content is a boundary too.
    preceding_state: "StateReport | None" = None
    source_position: int | None = None

    def candidates(self) -> list[str]:
        return [topic for topic, _ in self.topics.most_common(12)]


@dataclass
class StateReport:
    category: str
    text: str
    clause: str
    actor: str
    recipient: str
    ts: float
    entity: tuple[str, str, str]
    days: frozenset[str]
    times: frozenset[str]
    changes: int = 0


def _entity(clause: str) -> tuple[str, str, str] | None:
    """A specific reference, not the broad topic bucket 'meals' or 'travel'."""
    matches = list(_ENTITY.finditer(clause))
    if len(matches) != 1:
        return None
    match = matches[0]
    noun = match.group().lower()
    # Keep the complete subject phrase, including numeric IDs. A recognized
    # noun inside a longer subject is not evidence for the unqualified event.
    before = _CORRECTION.sub("", clause[:match.start()]).strip(" ,:").lower()
    before = re.sub(r"^(?:the|a|an)(?:\s+|$)", "", before)
    if _TIME.search(before) or _STATUS.search(before):
        return None
    tail = clause[match.end():]
    # IDs must be consumed before looking for dates: #2026-09-01 is an ID.
    code = _IDENTIFIER.match(tail)
    identifier = re.sub(r"\s+", "", code.group(1)).lower() if code else ""
    if code:
        tail = tail[code.end():]
    boundary = re.search(r"\b(?:is|are|was|were|has|will)\b", tail, re.I)
    ends = [m.start() for m in (boundary, _STATUS.search(tail), _TIME.search(tail)) if m]
    subject_tail = tail[:min(ends)] if ends else tail
    # Only the supported predicate grammar may be discarded. Unparsed venues,
    # companions, IDs or named move origins remain quoted independent reports.
    remainder = tail[min(ends):] if ends else ""
    for pattern in (_TIME, _AMOUNT, _STATUS, _NEGATIVE):
        remainder = pattern.sub(" ", remainder)
    grammar = set("is are was were has have been still now at on from to until next this last the a an and by for".split())
    if any(word.lower() not in grammar for word in _WORDS.findall(remainder)):
        return None
    subject_tail = re.sub(r"\b(?:on|at|from|to|until|next|this|last)\s*$", "", subject_tail.strip(), flags=re.I)
    qualifier = " ".join((before + " " + subject_tail.strip(" ,:.!? ")).lower().split())
    return noun, qualifier, identifier


def _implicit_correction(clause: str) -> bool:
    """Only genuinely elided references may borrow a previous subject."""
    body = _CORRECTION.sub("", clause).strip(" ,:.")
    # A pronoun is insufficient if the rest introduces another subject.
    if re.fullmatch(r"(?:it|that)\s+(?:is\s+)?(?:off|canceled|cancelled|confirmed|delayed|postponed)", body, re.I):
        return True
    if not _CORRECTION.search(clause) or not _AMOUNT.search(body):
        return False
    remainder = _AMOUNT.sub("", body)
    return bool(re.fullmatch(r"[\s,]*(?:(?:not|rather than|instead of)[\s,]*)?", remainder, re.I))


def _time_identity(times: list[str], ts: float, clause: str) -> tuple[frozenset[str], frozenset[str]]:
    """Calendar constraints cannot be satisfied by a shared clock token."""
    days, clocks = set(), set()
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    try:
        source = datetime.fromtimestamp(ts).date()
    except (ValueError, OverflowError, OSError):
        source = None
    for value in times:
        value = value.lower()
        if re.fullmatch(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2}", value):
            clocks.add(value.replace(" ", ""))
        elif source and value in {"today", "tonight", "tomorrow", "yesterday"}:
            days.add((source + timedelta(days={"tomorrow": 1, "yesterday": -1}.get(value, 0))).isoformat())
        elif source and value in weekdays:
            offset = (weekdays.index(value) - source.weekday()) % 7
            if re.search(rf"\blast\s+{value}\b", clause, re.I):
                offset = offset - 7 if offset else -7
            elif re.search(rf"\bthis\s+{value}\b", clause, re.I):
                offset = weekdays.index(value) - source.weekday()
            elif re.search(rf"\bnext\s+{value}\b", clause, re.I):
                offset = weekdays.index(value) - source.weekday() + 7
            days.add((source + timedelta(days=offset)).isoformat())
        else:
            days.add(value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else f"{value}@{_date(ts)}")
    return frozenset(days), frozenset(clocks)


def _schedule(clause: str, ts: float):
    """Separate explicitly stated old coordinates from the resulting schedule.

    Unsupported or multi-valued origins/destinations cannot establish a change.
    Keeping only destination coordinates prevents later matches to stale origins.
    """
    def coordinates(text):
        return _time_identity([m.group() for m in _TIME.finditer(text)], ts, text)

    subject = _ENTITY.search(clause)
    identifier = _IDENTIFIER.match(clause, subject.end()) if subject else None
    if identifier:
        clause = clause[:identifier.start()] + " " + clause[identifier.end():]
    move = _MOVED.search(clause)
    moving = bool(move or _CORRECTION.search(clause))
    origin, destination = "", clause
    valid = True
    if move:
        origin, destination = clause[:move.start()], clause[move.end():]
        explicit_from = re.search(r"\bfrom\b", destination, re.I)
        if explicit_from:
            parts = re.split(r"\bto\b", destination[explicit_from.end():], maxsplit=1, flags=re.I)
            origin += " " + parts[0]
            destination = parts[1] if len(parts) == 2 else ""
            valid = len(parts) == 2 and bool(_TIME.search(parts[0]))
    days, times = coordinates(destination)
    old_days, old_times = coordinates(origin)
    valid = valid and all(len(values) <= 1 for values in (days, times, old_days, old_times))
    return days, times, old_days, old_times, moving, valid


def _record_state(group: Conversation, report: StateReport, *, changing: bool,
                  moving: bool, implicit: bool, origin_days: frozenset[str],
                  origin_times: frozenset[str]) -> None:
    """Replace only a uniquely identified same-speaker report, across sections.

    Earlier text is explicitly previous context, never inherited current fact.
    Questions, conditions and proposed actions do not establish an override.
    """
    candidates = []
    for previous in group.states:
        if previous.actor != report.actor or previous.recipient != report.recipient:
            continue
        if implicit:
            if not previous.entity[0]:
                continue  # an unresolved pronoun cannot establish an event
            if not 0 <= report.ts - previous.ts <= 300:
                continue
            if bool(_AMOUNT.search(report.clause)) != bool(_AMOUNT.search(previous.clause)):
                continue
            if (report.times or report.days) and not (previous.times or previous.days):
                continue
        elif previous.entity != report.entity:
            continue
        # Explicit origins constrain identity even when the destination changes.
        # An absent old coordinate is not evidence of agreement.
        if origin_days and origin_days != previous.days:
            continue
        if origin_times and origin_times != previous.times:
            continue
        if previous.days and report.days and not moving and previous.days != report.days:
            continue
        if previous.times and report.times and not moving and previous.times != report.times:
            continue
        if (not (report.times or report.days) and report.entity[0] not in {"rent", "invoice", "payment"}
                and report.ts - previous.ts > 48 * 3600):
            continue  # an untimed cancellation weeks later may concern another event
        candidates.append(previous)
    # Multiple schedules for the same broad noun are ambiguous too. Don't pick
    # the most recent dinner when two distinct dinners could have been canceled.
    identities = {(p.entity, p.days, p.times) for p in candidates}
    if (changing and len(identities) == 1
            and (not implicit or candidates[-1] is group.preceding_state)):
        previous = candidates[-1]
        for old in candidates:
            group.signals[old.category].remove(old.text)
            group.states.remove(old)
        report.changes = previous.changes + len(candidates)
        report.entity = previous.entity if implicit else report.entity
        if not moving:
            report.times = report.times or previous.times
            report.days = report.days or previous.days
        report.text = (f"Latest report: {report.text}; previous report from {plain(previous.actor, 48)}"
                       f" [sent {_date(previous.ts)}]: {_proposition(previous.clause)}"
                       f" ({report.changes} earlier reports superseded)")
    elif changing and len(identities) > 1:
        report.text += "; several earlier events may match—no event assumed superseded"
    group.states.append(report)
    group.preceding_state = report if report.entity[0] else None
    group.signals[report.category].append(report.text)


def _subject(body: str) -> list[str]:
    objects = []
    for match in _OBJECT.finditer(body):
        phrase = re.split(r"\s+(?:by|at|on|before|after|for|with|to)\s+", match.group(1), maxsplit=1, flags=re.I)[0]
        phrase = _TIME.sub("", phrase).strip()
        words = _WORDS.findall(phrase)
        if words and words[0].lower() not in {"you", "me", "him", "her", "us", "them", "it", "that"}:
            objects.append(" ".join(words[:5]).lower())
    subjects = [name for name, pattern in _TOPICS.items() if re.search(pattern, body, re.I)]
    if subjects or objects:
        return list(dict.fromkeys(objects + subjects))[:3]
    # Sparse/unknown topics still get a useful structural subject without
    # copying sentences. No model or external lookup is required.
    words = [w.lower() for w in _WORDS.findall(body)
             if len(w) > 3 and w.lower() not in _STOP and not w.isdigit()]
    return list(dict.fromkeys(plain(w, 40) for w in words))[:3]


def split_sender(text: str) -> tuple[str, str]:
    # The cache writer uses ': '. A colon in 7:30 or a URL is not a
    # sender separator. Keep malformed rows unknown instead of guessing.
    match = re.match(r"^([^:\n]{1,100}):\s+(.+)$", text, re.S)
    return (match.group(1).strip(), match.group(2).strip()) if match else ("Unknown sender", text.strip())


def _date(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return "unknown date"


def _proposition(clause: str) -> str:
    """Preserve predicates, polarity and values; never replace them with tags.

    Longer statements retain bounded source spans around material words. Such
    excerpts are explicitly partial and cannot supersede complete earlier facts.
    """
    # Escape comparison operators for Markdown rather than erasing their meaning.
    clean = plain(clause.replace("<", "&lt;").replace(">", "&gt;"), MAX_BODY_CHARS, quoted=True).rstrip(".!?")
    if len(clean) <= 180:
        return f"“{clean}”"
    spans = [(0, min(40, len(clean)))]
    for pattern in (_STATUS, _NEGATIVE, _AMOUNT, _TIME):
        for match in pattern.finditer(clean):
            spans.append((max(0, match.start() - 35), min(len(clean), match.end() + 40)))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    excerpt = " … ".join(clean[start:end] for start, end in merged)
    if len(excerpt) > 240:
        return "Long factual statement; status/details omitted—review the conversation."
    return f"“{excerpt}” [statement shortened]"


def analyze(rows: list[tuple[float, str, str]], addressees: list[str]) -> list[Conversation]:
    groups: dict[str, Conversation] = {}
    # A sparse historical period needs an anchor even if all rows share a day.
    dated = {_date(ts) for ts, _, _ in rows} != {datetime.now().date().isoformat()}
    for source, recipient in sorted(zip(rows, addressees, strict=True), key=lambda pair: pair[0][0]):
        ts, context, text = source
        group = groups.setdefault(context, Conversation(context))
        group.count += 1
        if isinstance(source, SummaryRow):
            if source.source_before != group.source_position:
                group.preceding_state = None
            group.source_position = source.source_after
        elif group.source_position is not None:
            group.preceding_state = None
            group.source_position = None
        sender, body = split_sender(text)
        if _REACTION.fullmatch(body) or not _WORDS.search(body):
            group.reactions += 1
            continue
        truncated = len(body) > MAX_BODY_CHARS
        if truncated:
            group.truncated = True
        body = body[:MAX_BODY_CHARS]
        actor = "You" if sender == "Me" else plain(sender, 48)
        addressed = f" to {plain(recipient, 40)}" if recipient else ""
        source_day = f" [sent {_date(ts)}]" if dated else ""
        clauses = re.split(r"(?<=[.!?])\s+|;\s*|\n", body)
        for clause_index, clause in enumerate(clauses):
            clause = clause.strip()
            if not clause or _REACTION.fullmatch(clause):
                continue
            words = _WORDS.findall(clause.lower())
            if len(words) > 15 and len(set(words)) / len(words) < 0.25:
                group.preceding_state = None
                continue  # repeated notification/text noise, not a useful fact
            topics = _subject(clause)
            group.topics.update(topics)
            subject = ", ".join(plain(t, 24) for t in topics) or "the conversation"
            times = list(dict.fromkeys(m.group(0) for m in _TIME.finditer(clause)))[:3]
            time_detail = (f" ({', '.join(times)})" if times else "") + source_day
            qualifier = ("negative/declined " if _NEGATIVE.search(clause) else
                         "conditional " if _CONDITIONAL.search(clause) else "")
            decision = bool(_DECISION.search(clause))
            action = bool(_ACTION.search(clause))
            question = bool(_QUESTION.search(clause))
            plan = bool(_PLAN.search(clause) or times or _STATUS.search(clause) or _CORRECTION.search(clause))
            category, fact = "", ""
            recorded_state = False
            if decision:
                # Lexical evidence, never inferred acceptance. Qualifiers belong
                # to the decision clause, not another sentence in the message.
                verbs = list(dict.fromkeys(m.group(0).lower() for m in _DECISION.finditer(clause)))
                category = "Decisions mentioned"
                fact = (f"{actor}{addressed}: {qualifier}{', '.join(verbs[:2])} wording about {subject}"
                        f" — {_proposition(clause)}{time_detail}")
            if plan and not (decision or action or question):
                category = "Plans / times mentioned"
                fact = f"{actor}{addressed}: {_proposition(clause)}{time_detail}"
            if fact:
                reference = _entity(clause)
                implicit = reference is None and _implicit_correction(clause)
                certain = not (question or action or _CONDITIONAL.search(clause)) and len(clause) <= 180
                # A terminal fragment may hide a condition or another subject.
                certain = certain and not (truncated and clause_index == len(clauses) - 1)
                days, clocks, origin_days, origin_times, moving, valid = _schedule(clause, ts)
                if certain and valid and (reference or implicit):
                    report = StateReport(category, fact, clause, sender, recipient, ts,
                                         reference or ("", "", ""), days, clocks)
                    _record_state(group, report, changing=bool(_STATUS.search(clause) or _CORRECTION.search(clause)),
                                  moving=moving, implicit=implicit,
                                  origin_days=origin_days, origin_times=origin_times)
                    recorded_state = True
                else:
                    group.signals[category].append(fact)
            if not recorded_state:
                group.preceding_state = None
            if action:
                commitment = bool(re.search(r"\bi(?:'|’)ll\b|\bi will\b", clause, re.I))
                kind = "commitment" if commitment else "request"
                group.signals["Action items mentioned"].append(
                    f"{actor}{addressed}: {qualifier}{kind} — {_proposition(clause)}{source_day}")
            if question:
                scope = ("your question/request" if sender == "Me" else
                         f"question/request to {plain(recipient, 40)}" if recipient else
                         "group question/request" if context.startswith("Group") else
                         "question/request to you")
                detail = "" if action else f" about {subject} — {_proposition(clause)}{time_detail}"
                group.signals["Reply check"].append(f"{actor}: {scope}{detail}")
            if not (decision or action or question or plan):
                group.signals["Updates"].append(f"{actor}{addressed} shared {_proposition(clause)}{source_day}")
        if truncated:
            # An unread suffix may contain a different subject, even when the
            # analyzed prefix ends with a complete, established event report.
            group.preceding_state = None
    # Requests/decisions first; retain input order as a stable tiebreaker.
    return sorted(groups.values(), key=lambda g: (
        bool(g.signals["Reply check"]), bool(g.signals["Action items mentioned"]),
        bool(g.signals["Decisions mentioned"])), reverse=True)


def topic_request(groups: list[Conversation]) -> dict[str, list[str]]:
    return {str(i): g.candidates() for i, g in enumerate(groups[:MAX_CONVERSATIONS])
            if g.candidates()}


def validate_topics(value: object, candidates: dict[str, list[str]]) -> dict[str, list[str]]:
    """An allowlist is stronger than trying to detect paraphrased hallucinations.

    Model output cannot supply headings, prose, source lines, new names or
    decisions. Missing, extra, duplicate and echoed output all fail closed.
    """
    if not isinstance(value, dict) or set(value) != set(candidates):
        raise ValueError("invalid topic mapping")
    for key, topics in value.items():
        if (not isinstance(topics, list) or not 1 <= len(topics) <= MAX_TOPICS
                or any(not isinstance(t, str) or t not in candidates[key] for t in topics)
                or len(set(topics)) != len(topics)):
            raise ValueError("invalid topic selection")
    return value


def render(groups: list[Conversation], label: str, *, topics: dict[str, list[str]] | None = None,
           degraded: bool = False) -> str:
    if not groups:
        return f"No substantive messages found for {plain(label, 160)}."
    heading = f"💬 **Messages digest — {plain(label, 260)}**"
    count = sum(g.count for g in groups)
    output = [heading, f"{count} messages across {len(groups)} conversations."]
    if degraded:
        output.append("Basic digest: local topic selection was unavailable; details below use message structure.")
    output.append("Requests may already have replies; decisions and relative times are reported as mentioned, not verified outcomes.")
    shown = 0
    for i, group in enumerate(groups[:MAX_CONVERSATIONS]):
        selected = (topics or {}).get(str(i), group.candidates()[:MAX_TOPICS])
        section = [f"**{plain(group.label)}**"]
        if selected:
            section.append("Topics: " + ", ".join(plain(t, 24) for t in selected) + ".")
        else:
            section.append("Brief acknowledgments/reactions; no substantive topic detected."
                           if group.reactions == group.count else "Conversation updates; no clear topic detected.")
        for category, entries in group.signals.items():
            # Preserve the latest occurrence of repeated statuses. A final
            # cancellation may repeat an earlier cancellation after a revival.
            unique = list(dict.fromkeys(reversed(entries)))[::-1]
            if unique:
                # One aggregate line per category, never one line per message.
                selected_entries = ([unique[0], unique[-1]]
                                    if category == "Updates" and len(unique) > 2 else unique[-2:])
                excerpt = "; ".join(selected_entries)
                extra = f"; +{len(unique) - 2} other signals" if len(unique) > 2 else ""
                section.append(f"- {category}: {excerpt}{extra}.")
        if group.truncated:
            section.append("Long messages analyzed only in part.")
        section_text = "\n".join(section)
        # Reserve room for the explicit omission disclosure; never cut prose
        # mid-sentence or imply that omitted conversations had no activity.
        if len("\n\n".join(output)) + len(section_text) + 180 > MAX_OUTPUT_CHARS:
            break
        output.append(section_text)
        shown += 1
    if shown < len(groups):
        output.append(f"Showing {shown} of {len(groups)} conversations; {len(groups) - shown} more are included in the count. Ask for a narrower period for more detail.")
    return "\n\n".join(output)
