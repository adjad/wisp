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
from datetime import datetime

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
_CONDITIONAL = re.compile(r"\b(?:if|unless|whether|might|maybe|possibly|pending)\b", re.I)
_STOP = set("a an and are as at be been but by can could did do does for from had has have "
            "he her here him his how i if in is it its just me my of on or our she so some "
            "that the their them then there these they this to too us was we were what when "
            "where which who why will with would you your hi hey hello please thanks today "
            "tomorrow tonight yesterday am pm".split())


def plain(value: str, limit: int = 80) -> str:
    """Names/labels are data too: never allow Markdown or multiline framing."""
    value = re.sub(r"[\x00-\x1f\x7f*`\[\]<>#|\\]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


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

    def candidates(self) -> list[str]:
        return [topic for topic, _ in self.topics.most_common(12)]


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


def _update(body: str) -> str:
    # A short proposition is more useful than a noun index for ordinary news.
    # Keep it visibly attributed, preserve pronouns/negation, and bound it to
    # one clause (not an entire message or a reconstructed transcript).
    clause = re.split(r"(?<=[.!?])\s+|\n", body, maxsplit=1)[0]
    clause = re.sub(r"^(?:hi|hey|hello)[,!]?\s+", "", clause, flags=re.I)
    return plain(clause, 100).rstrip(".!?")


def analyze(rows: list[tuple[float, str, str]], addressees: list[str]) -> list[Conversation]:
    groups: dict[str, Conversation] = {}
    dated = len({_date(ts) for ts, _, _ in rows}) > 1
    for (ts, context, text), recipient in sorted(zip(rows, addressees, strict=True), key=lambda pair: pair[0][0]):
        group = groups.setdefault(context, Conversation(context))
        group.count += 1
        sender, body = split_sender(text)
        if _REACTION.fullmatch(body) or not _WORDS.search(body):
            group.reactions += 1
            continue
        if len(body) > MAX_BODY_CHARS:
            group.truncated = True
        body = body[:MAX_BODY_CHARS]
        actor = "You" if sender == "Me" else plain(sender, 48)
        addressed = f" to {plain(recipient, 40)}" if recipient else ""
        source_day = f" [sent {_date(ts)}]" if dated else ""
        for clause in re.split(r"(?<=[.!?])\s+|;\s*|\n", body):
            clause = clause.strip()
            if not clause or _REACTION.fullmatch(clause):
                continue
            words = _WORDS.findall(clause.lower())
            if len(words) > 15 and len(set(words)) / len(words) < 0.25:
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
            plan = bool(_PLAN.search(clause) or times)
            if decision:
                # Lexical evidence, never inferred acceptance. Qualifiers belong
                # to the decision clause, not another sentence in the message.
                verbs = list(dict.fromkeys(m.group(0).lower() for m in _DECISION.finditer(clause)))
                group.signals["Decisions mentioned"].append(
                    f"{actor}{addressed}: {qualifier}{', '.join(verbs[:2])} wording about {subject}{time_detail}")
            if plan and not (decision or action or question):
                group.signals["Plans / times mentioned"].append(f"{actor}{addressed}: {qualifier}{subject}{time_detail}")
            if action:
                commitment = bool(re.search(r"\bi(?:'|’)ll\b|\bi will\b", clause, re.I))
                kind = "commitment" if commitment else "request"
                group.signals["Action items mentioned"].append(
                    f"{actor}{addressed}: {qualifier}{kind} — “{_update(clause)}”{source_day}")
            if question:
                scope = ("your question/request" if sender == "Me" else
                         f"question/request to {plain(recipient, 40)}" if recipient else
                         "group question/request" if context.startswith("Group") else
                         "question/request to you")
                detail = "" if action else f" about {subject}{time_detail}"
                group.signals["Reply check"].append(f"{actor}: {scope}{detail}")
            if not (decision or action or question or plan):
                group.signals["Updates"].append(f"{actor}{addressed} shared “{_update(clause)}”{source_day}")
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
