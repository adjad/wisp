"""Deterministic compiler for grounded summary-delivery workflows."""
from __future__ import annotations

import re
from dataclasses import dataclass

from service.config import role_to_model
from service.reminder_intent import REMINDER_CREATE_RE
from service.router.router import RouteDecision
from service.workflows.models import WorkflowPlan


_EMAIL = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d().\s-]{6,}\d)(?!\w)")
_CANCEL = re.compile(r"^\s*(?:no|nope|never\s*mind|nevermind|cancel(?:\s+it)?|stop|forget\s+it)\s*[.!]?\s*$", re.I)
_ASSENT = re.compile(r"^\s*(?:(?:ok(?:ay)?|yes)\s+)?(?:yes|yep|yeah|ok(?:ay)?|sure|go ahead|do it|send it|try again|retry)\s*[.!]?\s*$", re.I)
_RELATION = r"mom|mother|dad|father|sister|brother|wife|husband|partner|boss"
_NAME = r"[A-Z][A-Za-z'\-]*(?:\s+[A-Z][A-Za-z'\-]*){0,2}"

SOURCE_TO_TOOL = {
    "calendar": "get_upcoming",
    "reminder": "search_reminders",
    "email": "summarize_emails",
    "messages": "summarize_messages",
    "stock": "get_stock_price",
    "news": "web_search",
    "daily_brief": "daily_brief",
    "weather": "get_weather",
}

_OUTBOUND = re.compile(
    r"\b(?:send|text|message|e-?mail|share|forward|draft|compose|write)\b", re.I)
CONTENT_QUESTION = ("I need a new delivery request with an exact content scope. "
                    "Which source and scope should I send to the recipient?")
_SUMMARY = re.compile(
    r"\b(?:summary|summaries|digest|report|brief|briefing|rundown|update|recap|"
    r"what(?:'s| is) (?:on|in)|updates|comparing|comparison|upcoming|schedule|agenda|movements?|prices?|headlines?)\b",
    re.I,
)

_MESSAGES_CHANNEL = re.compile(
    r"\b(?:via|through|using|on)\s+(?:i\s*message|imessages?|messages?|texts?)\b|"
    r"\b(?:i\s*message|imessages?)\b|"
    r"\b(?:text|message)\s+(?:it\s+)?(?:to\s+)?", re.I)
_EMAIL_CHANNEL = re.compile(
    r"\b(?:via|through|using|on)\s+(?:e-?mail|mail)\b|"
    r"\b(?:send\s+(?:an?\s+)?e-?mail|e-?mail)\s+(?:to\s+)?(?:me\b|myself\b|"
    rf"{_RELATION}\b|[A-Z0-9._%+\-]+@)", re.I)
_EMAIL_PROPER_NAME = re.compile(
    r"(?i:\b(?:send\s+(?:an?\s+)?e-?mail|e-?mail)\s+(?:to\s+)?)"
    r"[A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+){0,2}\b")

_DRAFT = re.compile(r"\b(?:draft|compose|write)\b|\b(?:do not|don't|dont)\s+send\b|\bunsent\b", re.I)
_SCHEDULE = re.compile(
    r"\bschedule\b[^.?!]{0,80}\b(?:send|text|message|e-?mail)\b|"
    r"\b(?:send|text|message|e-?mail)\b[^.?!]{0,100}\b(?:"
    r"at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|tonight\s+at|"
    r"tomorrow\s+at|next\s+\w+\s+at)\b", re.I)

_RANGE_PATTERNS = [
    re.compile(r"\b(?:today|tomorrow|yesterday)\b", re.I),
    re.compile(r"\b(?:this|last|next|past)\s+(?:day|week|month|year|weekend)\b", re.I),
    re.compile(r"\b(?:past|last|next)\s+\d+\s+(?:days?|weeks?|months?)\b", re.I),
]
_QUOTED_SPAN = re.compile(
    r'"(?:\\.|[^"\\])*"|“[^”]*”|'
    r"(?<!\w)'(?:[^']|(?<=\w)'(?=\w))*'(?!\w)|"
    r"(?<!\w)‘(?:[^’]|(?<=\w)’(?=\w))*’(?!\w)")
_WHEN = re.compile(
    r"\b(?:(?:today|tomorrow|tonight|next\s+\w+)\s+)?at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b(?:\s+(?:today|tomorrow))?|"
    r"\b(?:tomorrow|tonight)\s+(?:morning|afternoon|evening)\b", re.I)

_INLINE_EMAIL_SUMMARY = re.compile(
    r"^(?:can\s+you\s+|could\s+you\s+|please\s+)?(?:"
    r"(?:send|show|give|tell)\s+me\s+(?:my\s+|the\s+)?"
    r"(?:e-?mail|inbox)\s+(?:summary|summaries|digest|recap)|"
    r"what\s+are\s+(?:my\s+|the\s+)?(?:e-?mail|inbox)\s+"
    r"(?:summary|summaries|digest|recap))"
    r"(?:\s+(?:for|from)\s+(?:today|yesterday|"
    r"(?:this|last|past)\s+(?:day|week|month|year)|"
    r"(?:last|past)\s+\d+\s+(?:days?|weeks?|months?)|"
    r"\d{4}-\d{2}(?:-\d{2})?))?[\s.!?]*$", re.I)


def _normalize(text: str) -> str:
    replacements = {
        r"\bcalender\b": "calendar",
        r"\bscedule\b": "schedule",
        r"\bschedual\b": "schedule",
        r"\b(?:tmrow|tmrw)\b": "tomorrow",
        r"\bimsg\b": "Messages",
        r"\btexxt\b": "text",
        r"\bportfoliio\b": "portfolio",
    }
    value = text
    for pattern, replacement in replacements.items():
        value = re.sub(pattern, replacement, value, flags=re.I)
    return value

def is_cancel(text: str) -> bool:
    return bool(_CANCEL.match(text))


def is_assent(text: str) -> bool:
    return bool(_ASSENT.match(text))


def extract_channel(text: str) -> str:
    value = _normalize(text.strip())
    if re.fullmatch(r"(?:i\s*message|imessages?|messages?|texts?)(?:\s+message)?\.?", value, re.I):
        return "messages"
    if re.search(r"\ba\s+Messages\s+(?:update|summary|report)\b", value, re.I):
        return "messages"
    if re.fullmatch(r"(?:e-?mail|mail)\.?", value, re.I):
        return "email"
    if _MESSAGES_CHANNEL.search(value):
        return "messages"
    if (re.search(r"\bsend\b[^.?!]{0,50}\ban?\s+e-?mail\b", value, re.I)
            or re.search(r"\bto\s+my\s+(?:e-?mail|inbox)\b", value, re.I)
            or _EMAIL.search(value) or _EMAIL_CHANNEL.search(value)
            or _EMAIL_PROPER_NAME.search(value)):
        return "email"
    return ""


# "send/text/share <anything> to <name>" — the artifact-first phrasing.
_DELIVER_TO = re.compile(
    r"\b(?:send|text|message|e-?mail|share|forward)\b[^.?!]{0,80}?"
    r"\b(?:to|with)\s+(?:my\s+)?"
    r"(?P<name>[A-Za-z][A-Za-z'\-]{0,30}(?:\s+[A-Za-z][A-Za-z'\-]{0,30}){0,2})",
    re.I)
# A name ends where the rest of the sentence begins.
_NAME_STOP = frozenset({
    "for", "with", "about", "saying", "that", "via", "through", "using",
    "on", "in", "at", "and", "or", "by", "from", "the", "a", "an", "as",
    "this", "next", "last", "today", "tomorrow", "tonight", "please", "asap",
    "now", "again", "only", "instead", "later", "immediately", "right",
    "then", "my", "our", "your", "their", "his", "her",
})
# Things that are never a person: the payload, the channel, or the user
# themselves. Without this, "send my calendar to my email" would text "email".
_NOT_A_PERSON = frozenset({
    "message", "messages", "text", "texts", "imessage", "email", "emails",
    "mail", "e-mail", "inbox", "calendar", "schedule", "agenda", "summary",
    "summaries", "report", "reports", "digest", "brief", "briefing", "news",
    "reminder", "reminders", "notes", "everyone", "them", "him", "her", "it",
    "me", "myself", "us", "group", "work", "number", "phone", "contact",
    "contacts", "trash", "archive",
    "section", "part", "version",
})


def _trim_name(raw: str) -> str:
    """The person out of a loose match, or "" if it isn't one."""
    words: list[str] = []
    for word in raw.split():
        if word.lower() in _NAME_STOP:
            break
        words.append(word)
    if not 1 <= len(words) <= 3:
        return ""
    if any(word.lower().strip(".,'") in _NOT_A_PERSON for word in words):
        return ""
    return " ".join(words)


def extract_recipient(text: str, channel: str = "") -> str:
    text = _normalize(text)
    if match := _EMAIL.search(text):
        return match.group(0)
    if match := _PHONE.search(text):
        return match.group(0).strip()
    if re.search(r"\b(?:myself|to me|to my (?:e-?mail|inbox)|email me|message me|text me)\b", text, re.I):
        return "me"

    relation_patterns = [
        rf"\bto\s+(?:my\s+)?(?P<name>{_RELATION})\b",
        rf"\b(?:text|message|e-?mail)\s+(?:my\s+)?(?P<name>{_RELATION})\b",
        rf"\bsend\s+(?:my\s+)?(?P<name>{_RELATION})\b",
    ]
    for pattern in relation_patterns:
        if match := re.search(pattern, text, re.I):
            return match.group("name").strip()

    # Lowercase contact names are normal user input. The older proper-name
    # branch intentionally required capitals and therefore lost "trishe" in
    # the exact phrase "send a message to trishe with ...". Keep lowercase
    # extraction bounded by compose grammar so payload nouns cannot become a
    # recipient.
    bounded_name_patterns = [
        r"\b(?:send|text|message|email)\s+(?:this|that|it)\s+(?:to\s+)?"
        r"(?P<name>[A-Za-z][A-Za-z'\-]{0,30}(?:\s+[A-Za-z][A-Za-z'\-]{0,30}){0,2})"
        r"(?=\s+(?:via|through|using)\b|[,.!?]|$)",
        r"\b(?:send|draft|compose|write)\s+(?:an?\s+)?(?:i\s*message|message|text|e-?mail)"
        r"\s+to\s+(?:my\s+)?(?P<name>[A-Za-z][A-Za-z'\- ]{0,60}?)"
        r"(?=\s+(?:with|about|saying|that)\b|[,.!?]|$)",
        r"\b(?:send|text|message|e-?mail)\s+(?:my\s+)?"
        r"(?P<name>[A-Za-z][A-Za-z'\- ]{0,60}?)\s+(?:an?\s+)?"
        r"(?:i\s*message|message|text|e-?mail)\b",
        r"\be-?mail\s+(?:to\s+)?(?P<name>[A-Za-z][A-Za-z'\-]{0,30})"
        r"(?=\s+(?:my|the|a|an|with|about)\b|$)",
    ]
    for pattern in bounded_name_patterns:
        if match := re.search(pattern, text, re.I):
            # Trimmed by the same rule as the artifact-first pattern below: a
            # greedy three-word capture otherwise swallows the tail of the
            # sentence, and "send this to trishe via messages" addressed a
            # contact named "trishe via messages".
            if name := _trim_name(match.group("name")):
                return name

    # EVERY pattern above assumes the sentence's direct object is the word
    # "message"/"text"/"email" — "send a message to trishe with my calendar".
    # MEASURED FAILURE (2026-09-08, live): "send my calender to trishe(the next
    # two weeks)" names the ARTIFACT as the object instead, so none of them fit,
    # the case-sensitive proper-name branch below skipped a lowercase "trishe",
    # and Wisp asked "Who should I message it to?" about a person the user had
    # already named in their very first sentence.
    #
    # So: any delivery verb, then "to"/"with", then a name — case-insensitive,
    # with the payload nouns and the sentence's continuation stripped off by
    # _trim_name rather than by a lookahead. A lookahead was what lost this one
    # in the first place: "trishe(the" has no space before its terminator.
    if match := _DELIVER_TO.search(text):
        if name := _trim_name(match.group("name")):
            return name

    # Keep the proper-name portion case-sensitive. Compiling the whole pattern
    # with re.I makes ordinary payload words ("email summaries") look like a
    # capitalized contact and was one of the ambiguities this compiler exists
    # to remove.
    proper_patterns = [
        rf"(?i:\bto\s+(?:my\s+)?)(?P<name>{_NAME})\b",
        rf"(?i:\b(?:text|message|e-?mail)\s+(?:my\s+)?)(?P<name>{_NAME})\b",
        rf"(?i:\bsend\s+(?:my\s+)?)(?P<name>{_NAME})\s+(?:a\s+)?"
        rf"(?:(?i:stock|calendar|e-?mail|news)\s+)?"
        rf"(?i:message|text|e-?mail|summary|report|digest|brief|my\b|the\b)",
    ]
    banned = {"message", "messages", "email", "emails", "mail", "calendar", "schedule",
              "summary", "summaries", "report", "reports"}
    for pattern in proper_patterns:
        if match := re.search(pattern, text):
            name = match.group("name").strip()
            if name.lower() not in banned:
                return name
    return ""


def _date_range(text: str) -> str:
    for pattern in _RANGE_PATTERNS:
        if match := pattern.search(text):
            return match.group(0).lower()
    return ""


_CALENDAR_EXTENDED_RANGE = re.compile(
    r"\b(?:the\s+)?(?:this\s+week\s+and\s+next\s+week|"
    r"next\s+(?:two|2)\s+weeks?|"
    r"(?:next|this|past|last)\s+(?:few|three|3)\s+weeks?)\b", re.I)


def extract_location(text: str, *, standalone: bool = False) -> str:
    value = text.strip().strip(".,")
    value = re.sub(r"^(?:(?:it(?:'s| is)\s+)?(?:for|in)\s+)", "", value, flags=re.I)
    if is_temporal_location(value):
        return ""
    if standalone and re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,60}(?:,\s*[A-Z]{2})?", value):
        return value
    match = re.search(
        r"\b(?:weather|forecast)\b[^.?!]{0,50}\b(?:in|for)\s+"
        r"(?P<place>[A-Za-z][A-Za-z .'-]{1,50}(?:,\s*[A-Z]{2})?)"
        r"(?=\s+(?:today|tomorrow|this|next|last|past|and\b)|[,.?!]|$)",
        text, re.I)
    if not match:
        return ""
    place = match.group("place").strip()
    place = re.split(r"\s+(?:via|through|using|and|today|tomorrow)\b", place,
                     maxsplit=1, flags=re.I)[0].strip()
    if is_temporal_location(place):
        return ""
    return place


def is_temporal_location(value: str) -> bool:
    """Reject a time slot used as a place, at compilation and tool dispatch."""
    return bool(re.fullmatch(
        r"(?:(?:for|in)\s+)?(?:today|tomorrow|yesterday|tonight|now|"
        r"(?:this|next|last|past|coming)(?:\s+\d+)?\s+"
        r"(?:days?|weeks?|months?|years?|weekend|morning|afternoon|evening)|"
        r"(?:the\s+)?(?:next|last)\s+\w+day)", value.strip(), re.I))


def extract_stock_symbols(text: str, *, standalone: bool = False) -> list[str]:
    for company, ticker in {"nvidia": "NVDA", "apple": "AAPL", "microsoft": "MSFT",
                            "amazon": "AMZN", "tesla": "TSLA", "google": "GOOGL",
                            "amd": "AMD", "micron": "MU"}.items():
        text = re.sub(rf"\b{company}\b", ticker, text, flags=re.I)
    tickers = re.findall(r"(?<![A-Za-z])\$?([A-Z]{1,5})(?![A-Za-z])", text)
    tickers = [s for s in tickers if s not in {"I", "A", "THE", "SEND", "EMAIL"}]
    value = text.strip().strip(".,")
    if not standalone:
        match = re.search(
            r"\b(?:share|stock)\s+prices?\s+(?:of|for)\s+(?P<names>.+?)"
            r"(?=\s+(?:from|over|during|today|yesterday|tomorrow|this|last|past|"
            r"via|through|using)\b|[.?!]|$)", text, re.I)
        if not match:
            return list(dict.fromkeys(tickers))
        value = match.group("names")
    if len(value) > 100:
        return []
    parts = [part.strip(" ,") for part in re.split(r"\s*(?:,|\band\b)\s*", value, flags=re.I)]
    banned = {"", "my stock", "my stocks", "stock", "stocks", "share", "shares",
              "today", "tomorrow", "yesterday", "now", "current", "this week",
              "last week", "this month", "last month"}
    return list(dict.fromkeys(part for part in parts
                              if part.lower() not in banned
                              and re.fullmatch(r"[A-Za-z][A-Za-z .&'-]{0,35}", part)))[:10]


def _reminder_query(text: str) -> str:
    match = re.search(
        r"\bwith\s+(?:my\s+)?(?P<query>[A-Za-z0-9][A-Za-z0-9 '\-]{0,50}?)"
        r"\s+information\s+from\s+(?:my\s+)?(?:apple\s+)?reminders?\b",
        text, re.I)
    if not match:
        match = re.search(
            r"\b(?P<query>(?:[A-Za-z0-9][A-Za-z0-9'\-]*\s+)"
            r"{0,3}[A-Za-z0-9][A-Za-z0-9'\-]*)\s+"
            r"(?:information\s+)?from\s+(?:my\s+)?(?:apple\s+)?reminders?\b",
            text, re.I)
    query = " ".join(match.group("query").split()) if match else ""
    while re.match(r"^(?:with|about|the|my)\s+", query, re.I):
        query = re.sub(r"^(?:with|about|the|my)\s+", "", query,
                       count=1, flags=re.I)
    return query


_CONSTRAINT_INTRODUCER = re.compile(
    r"\b(?:only|just|limited\s+to|restricted\s+to|specifically|"
    r"exclusively|solely)\b", re.I)


def _following_delivery_boundary(text: str, start: int) -> int | None:
    starts = [match.start() for match in re.finditer(
        r"\b(?:via|through|using|by|as)\s+(?:an?\s+)?(?:apple\s+)?"
        r"(?:messages?|texts?|imessage|sms|e-?mail|mail)\b", text, re.I)
        if match.start() >= start]
    recipient = extract_recipient(text)
    if recipient:
        starts.extend(match.start() for match in re.finditer(
            rf"\b(?:to|with)\s+(?:my\s+)?{re.escape(recipient)}\b", text, re.I)
            if match.start() >= start)
    return min(starts) if starts else None


def _constraint_end(text: str, limiter_end: int, next_start: int | None) -> int:
    candidates = [len(text)]
    if next_start is not None:
        candidates.append(next_start)
    if (boundary := _following_delivery_boundary(text, limiter_end)) is not None:
        candidates.append(boundary)
    return min(candidates)


@dataclass(frozen=True)
class SourceConstraintLedger:
    scopes: dict[str, str]
    selected_sources: frozenset[str] | None
    spans: tuple[tuple[int, int], ...]
    candidate_count: int
    consumed_count: int
    residue: str
    error: bool

    @property
    def complete(self) -> bool:
        return (not self.error and not self.residue
                and self.candidate_count == self.consumed_count)


@dataclass(frozen=True)
class SourceClause:
    source: str
    noun_start: int
    noun_end: int
    start: int
    end: int


@dataclass(frozen=True)
class RequestSegmentation:
    """One lexical partition used by every post-selection validator."""
    text: str
    clauses: tuple[SourceClause, ...]
    constraint_spans: tuple[tuple[int, int], ...]

    def unconstrained_slice(self, start: int, end: int) -> str:
        chars = list(self.text[start:end])
        for span_start, span_end in self.constraint_spans:
            for index in range(max(span_start, start) - start,
                               min(span_end, end) - start):
                if 0 <= index < len(chars):
                    chars[index] = " "
        return "".join(chars)

    def retained_text(self, sources: set[str]) -> str:
        chars = list(self.text)
        spans = list(self.constraint_spans)
        spans.extend((clause.start, clause.end) for clause in self.clauses
                     if clause.source not in sources)
        if ("messages" not in sources
                and any(clause.source == "messages" for clause in self.clauses)):
            unused_conversation, conversation_span = (
                _message_conversation_binding(self.text))
            if conversation_span:
                spans.append(conversation_span)
        for start, end in spans:
            for index in range(max(0, start), min(end, len(chars))):
                chars[index] = " "
        retained = "".join(chars)
        retained = re.sub(
            r"^(\s*(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)*"
            r"(?:(?:only|just)\s+)?"
            r"(?:send|text|message|e-?mail|share|forward|draft|compose|write|schedule)\b)"
            r"\s+(?:and|plus|with|along\s+with)\b",
            r"\1 ", retained, flags=re.I)
        return retained

    def source_text(self, source: str, retained_sources: set[str]) -> str:
        clauses = [self.unconstrained_slice(item.start, item.end)
                   for item in self.clauses
                   if item.source == source and item.source in retained_sources]
        if not clauses and source in retained_sources:
            return self.retained_text(retained_sources)
        if len(retained_sources) == 1 and clauses:
            # A range outside the noun phrase is unambiguously owned only for
            # a single-source request (for example, after the recipient).
            return self.retained_text(retained_sources)
        return " ".join(clauses)


def _request_segmentation(
        text: str, constraint_spans: tuple[tuple[int, int], ...]) -> RequestSegmentation:
    mentions = [item for item in _payload_source_mentions(text)
                if not any(item[0] < end and item[1] > start
                           for start, end in constraint_spans)]
    if not mentions:
        return RequestSegmentation(text, (), constraint_spans)
    effect = re.match(
        r"^\s*(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)*"
        r"(?:(?:only|just)\s+)?"
        r"(?:send|text|message|e-?mail|share|forward|draft|compose|write|schedule)\b",
        text, re.I)
    envelope_starts = [match.start() for match in re.finditer(
        r"\b(?:via|through|using|by|as)\s+(?:an?\s+)?(?:apple\s+)?"
        r"(?:messages?|texts?|imessage|sms|e-?mail|mail)\b", text, re.I)]
    recipient = extract_recipient(text)
    if recipient:
        envelope_starts.extend(match.start() for match in re.finditer(
            rf"\b(?:to|with)\s+(?:my\s+)?{re.escape(recipient)}\b", text, re.I))
    clauses: list[SourceClause] = []
    connector_pattern = re.compile(r"\b(?:and|plus|with|along\s+with)\b", re.I)
    for index, (noun_start, noun_end, source) in enumerate(mentions):
        if index == 0:
            start = effect.end() if effect else noun_start
        else:
            prior_end = mentions[index - 1][1]
            bridge = text[prior_end:noun_start]
            connectors = list(connector_pattern.finditer(bridge))
            if connectors:
                start = prior_end + connectors[-1].start()
            elif source == "daily_brief" and (
                    relation := re.search(r"\bof\s+(?:my\s+|the\s+)?$", bridge, re.I)):
                start = prior_end + relation.start()
            else:
                start = noun_start
        if index + 1 < len(mentions):
            next_start = mentions[index + 1][0]
            connectors = list(connector_pattern.finditer(
                text[noun_end:next_start]))
            connector = connectors[-1] if connectors else None
            end = noun_end + connector.start() if connector else next_start
        else:
            following = [item for item in envelope_starts if item >= noun_end]
            end = min(following) if following else len(text)
        clauses.append(SourceClause(source, noun_start, noun_end, start, end))
    return RequestSegmentation(text, tuple(clauses), constraint_spans)


def _source_owned_date_ranges(
        segmentation: RequestSegmentation,
        sources: list[str]) -> tuple[dict[str, str], bool]:
    retained = set(sources)
    ranges: dict[str, str] = {}
    error = False
    for source in sources:
        values = []
        for clause in segmentation.clauses:
            if clause.source != source:
                continue
            value = _date_range(segmentation.unconstrained_slice(
                clause.start, clause.end))
            if value and value not in values:
                values.append(value)
        if len(retained) == 1 and not values:
            value = _date_range(segmentation.retained_text(retained))
            if value:
                values.append(value)
        if len(values) > 1:
            error = True
        ranges[source] = values[0] if len(values) == 1 else ""
    return ranges, error


def _has_explicit_owned_date_range(
        segmentation: RequestSegmentation,
        source_ranges: dict[str, str]) -> bool:
    """Whether a determiner-owned source phrase explicitly carries its range."""
    nouns = {
        "calendar": r"calendar|schedule|agenda",
        "email": r"e-?mails?|mail|inbox",
        "messages": r"messages?|texts?",
        "weather": r"weather|forecast",
        "news": r"news|headlines?",
        "stock": r"stocks?|shares?|portfolio",
    }
    for clause in segmentation.clauses:
        date_range = source_ranges.get(clause.source, "")
        noun = nouns.get(clause.source)
        if not date_range or not noun:
            continue
        value = segmentation.unconstrained_slice(clause.start, clause.end)
        if re.search(
                rf"\b(?:my|the|all)\s+(?:{noun})\b[^,;.!?]*"
                rf"\b(?:for|from|during|on)\s+{re.escape(date_range)}\b",
                value, re.I):
            return True
    return False


def _source_adjacent_residue(
        segmentation: RequestSegmentation, sources: list[str]) -> str:
    """Reject unowned words inside retained calendar source clauses."""
    retained = set(sources)
    for clause in segmentation.clauses:
        if clause.source != "calendar" or clause.source not in retained:
            continue
        tail = segmentation.unconstrained_slice(clause.noun_end, clause.end)
        tail = re.sub(
            r"\b(?:section|part|summary|summaries|digest|report|recap|brief|"
            r"briefing|list|events?|appointments?)\b", " ", tail, flags=re.I)
        for pattern in _RANGE_PATTERNS:
            tail = pattern.sub(" ", tail)
        tail = _CALENDAR_EXTENDED_RANGE.sub(" ", tail)
        tail = re.sub(r"[,.!?();:]", " ", tail)
        tail = re.sub(
            r"\b(?:for|on|during|and|but|please|now|immediately)\b", " ",
            tail, flags=re.I)
        residue = " ".join(tail.split())
        if residue:
            return residue
    return ""


def _source_constraint_ledger(
        text: str) -> tuple[dict[str, str], bool, list[tuple[int, int]]]:
    """Consume every post-source limiting span into one enforceable owner."""
    mentions = _payload_source_mentions(text)
    selected = {item[2] for item in mentions}
    if not mentions:
        return {}, False, []
    if marker := re.search(r"__UNSUPPORTED_QUOTED_[A-Z_]+__", text):
        return {}, True, [marker.span()]
    first_source_end = min(item[1] for item in mentions)
    limiters = [match for match in _CONSTRAINT_INTRODUCER.finditer(text)
                if match.start() >= first_source_end]
    if not limiters:
        return {}, False, []

    owner_nouns = (
        ("reminder", r"reminders?"),
        ("calendar", r"calendar|schedule|agenda|events?|appointments?"),
        ("email", r"e-?mails?|mail|inbox"),
        ("messages", r"messages?|texts?"),
        ("stock", r"stocks?|shares?|portfolio"),
        ("news", r"news|headlines?"),
        ("weather", r"weather|forecast"),
    )
    scopes: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    consumed = 0
    for index, limiter in enumerate(limiters):
        next_start = (limiters[index + 1].start()
                      if index + 1 < len(limiters) else None)
        end = _constraint_end(text, limiter.end(), next_start)
        body = text[limiter.end():end].strip().strip(".,;:!?").strip()
        body = re.sub(r"\b(?:and|but)\s*$", "", body, flags=re.I).strip()
        span_start = limiter.start()
        if lead := re.search(
                r"(?:[,;:]\s*)?\bbut\s+$", text[:limiter.start()], re.I):
            span_start = lead.start()
        span = (span_start, end)
        spans.append(span)
        if not body:
            return scopes, True, spans

        owners = {source for source, noun in owner_nouns
                  if re.search(rf"\b(?:{noun})\b", body, re.I)}
        if len(owners) > 1:
            return scopes, True, spans
        owner = next(iter(owners), "")
        if not owner and re.search(r"\bdue\b|\b(?:urgent|overdue|incomplete)\b", body, re.I):
            owner = "reminder"
        if not owner and len(selected) == 1:
            owner = next(iter(selected))
        if not owner or owner not in selected:
            return scopes, True, spans

        scoped_body = body
        if owner == "reminder":
            scoped_body = re.sub(
                r"^(?:(?:my|the|all)\s+)?reminders?\s+",
                "", scoped_body, count=1, flags=re.I)
            match = re.fullmatch(
                r"(?:(?:the\s+)?(?:ones?|those|items?)\s+)?"
                r"(?:due\s+)?(?P<scope>today|tomorrow)(?:'s|’s)?",
                scoped_body, re.I)
        elif owner == "calendar":
            scoped_body = re.sub(
                r"^(?:(?:my|the|all)\s+)?(?:calendar|schedule|agenda)"
                r"(?:\s+(?:events?|appointments?|section|part))?\s*",
                "", scoped_body, count=1, flags=re.I)
            match = re.fullmatch(
                r"(?:(?:for|on)\s+)?(?P<scope>today|tomorrow)(?:'s|’s)?",
                scoped_body, re.I)
        else:
            match = None

        if match:
            scope = match.group("scope").lower()
            if owner in scopes and scopes[owner] != scope:
                return scopes, True, spans
            scopes[owner] = scope
            consumed += 1
            continue

        # "only the email section" selects a source; it is not a filter.
        selection = next((noun for source, noun in owner_nouns if source == owner), "")
        if selection and re.fullmatch(
                rf"(?:(?:my|the|all)\s+)?(?:{selection})"
                r"(?:\s+(?:part|section))?", body, re.I):
            consumed += 1
            continue
        return scopes, True, spans

    return scopes, consumed != len(limiters), spans


def _trailing_constraint_residue(
        text: str, spans: list[tuple[int, int]]) -> str:
    mentions = [item for item in _payload_source_mentions(text)
                if not any(item[0] < end and item[1] > start
                           for start, end in spans)]
    if not mentions:
        return ""
    last_source_end = max(item[1] for item in mentions)
    boundaries = []
    for match in re.finditer(
            r"\b(?:via|through|using|by|as)\s+(?:an?\s+)?(?:apple\s+)?"
            r"(?:messages?|texts?|imessage|sms|e-?mail|mail)\b",
            text, re.I):
        if match.end() >= last_source_end:
            boundaries.append(match.end())
    recipient = extract_recipient(text)
    if recipient:
        for match in re.finditer(
                rf"\b(?:to|with)\s+(?:my\s+)?{re.escape(recipient)}\b",
                text, re.I):
            if match.end() >= last_source_end:
                boundaries.append(match.end())
    if not boundaries:
        return ""
    boundary = max(boundaries)
    chars = list(text[boundary:])
    for start, end in (*spans, *_private_qualifier_spans(text)):
        for index in range(max(start, boundary) - boundary,
                           max(end, boundary) - boundary):
            if 0 <= index < len(chars):
                chars[index] = " "
    residue = "".join(chars)
    calendar_source = any(item[2] == "calendar" for item in mentions)
    if calendar_source:
        residue = _CALENDAR_EXTENDED_RANGE.sub(" ", residue)
        residue = re.sub(r"[()]", " ", residue)
    residue = _WHEN.sub(" ", residue)
    if date_range := _date_range(residue):
        residue = re.sub(
            rf"\b(?:for|from|during|on)\s+{re.escape(date_range)}\b|"
            rf"\b{re.escape(date_range)}\b",
            " ", residue, flags=re.I)
    if location := extract_location(text):
        residue = re.sub(
            rf"\b(?:in|for)\s+{re.escape(location)}\b",
            " ", residue, flags=re.I)
    if calendar_source:
        residue = re.sub(r"\b(?:for|during|on)\b", " ", residue, flags=re.I)
    residue = re.sub(r"[,.!?();:]", " ", residue)
    residue = re.sub(
        r"\b(?:and|but|please|now|immediately)\b", " ", residue, flags=re.I)
    return " ".join(residue.split())


def _complete_source_constraint_ledger(text: str) -> SourceConstraintLedger:
    scopes, error, spans = _source_constraint_ledger(text)
    mentions = _payload_source_mentions(text)
    first_source_end = min((item[1] for item in mentions), default=len(text))
    candidates = [match for match in _CONSTRAINT_INTRODUCER.finditer(text)
                  if match.start() >= first_source_end]
    marker_count = len(re.findall(r"__UNSUPPORTED_QUOTED_[A-Z_]+__", text))
    selections = set()
    owner_nouns = (
        ("reminder", r"reminders?"),
        ("calendar", r"calendar|schedule|agenda|events?|appointments?"),
        ("email", r"e-?mails?|mail|inbox"),
        ("messages", r"messages?|texts?"),
        ("stock", r"stocks?|shares?|portfolio"),
        ("news", r"news|headlines?"),
        ("weather", r"weather|forecast"),
    )
    for index, candidate in enumerate(candidates):
        next_start = (candidates[index + 1].start()
                      if index + 1 < len(candidates) else None)
        end = _constraint_end(text, candidate.end(), next_start)
        body = text[candidate.end():end].strip().strip(".,;:!?").strip()
        body = re.sub(r"\b(?:and|but)\s*$", "", body, flags=re.I).strip()
        for source, noun in owner_nouns:
            if re.fullmatch(
                    rf"(?:(?:my|the|all)\s+)?(?:{noun})"
                    r"(?:\s+(?:part|section))?", body, re.I):
                selections.add(source)
    if len(selections) > 1:
        error = True
    selected_sources = frozenset(selections) if selections else None
    residue = _trailing_constraint_residue(text, spans)
    candidate_count = len(candidates) + marker_count
    consumed_count = candidate_count if not error else min(len(spans), candidate_count)
    return SourceConstraintLedger(
        scopes=dict(scopes),
        selected_sources=selected_sources,
        spans=tuple(spans),
        candidate_count=candidate_count,
        consumed_count=consumed_count,
        residue=residue,
        error=error,
    )


def _sentence_reminder_restriction(
        text: str) -> tuple[str, bool, tuple[int, int] | None]:
    ledger = _complete_source_constraint_ledger(text)
    spans = ledger.spans
    span = ((min(item[0] for item in spans), max(item[1] for item in spans))
            if spans else None)
    return ledger.scopes.get("reminder", ""), not ledger.complete, span


def _reminder_source_args(text: str) -> dict | None:
    """Consume a complete reminder noun phrase into enforceable tool args."""
    value = " ".join(_unquoted_scope_text(_normalize(text)).split())
    sentence_scope, sentence_error, sentence_span = (
        _sentence_reminder_restriction(value))
    if sentence_error:
        return None
    mentions = _payload_source_mentions(value)
    if sentence_span:
        mentions = [item for item in mentions
                    if not (item[0] < sentence_span[1]
                            and item[1] > sentence_span[0])]
    reminder_mentions = [
        (index, item) for index, item in enumerate(mentions)
        if item[2] == "reminder"
    ]
    if not reminder_mentions:
        return None
    restriction_args = None
    if len(reminder_mentions) > 1:
        if len(reminder_mentions) != 2:
            return None
        second_index, second = reminder_mentions[1]
        prior = mentions[second_index - 1]
        bridge = value[prior[1]:second[0]]
        if not re.search(r"\bbut\s+only\s*$", bridge, re.I):
            return None
        restriction_args = _reminder_source_args("send " + value[second[0]:])
        if (restriction_args is None
                or (restriction_args["scope"] == "all"
                    and not restriction_args["query"])):
            return None
    reminder = reminder_mentions[0][1]
    start, end, _ = reminder
    query = _reminder_query(value)
    scope = "all"

    if not query:
        prefix = value[:start]
        previous = [item for item in mentions if item[1] <= start]
        if previous:
            bridge = value[previous[-1][1]:start]
            connectors = list(re.finditer(
                r"\b(?:and|plus|with|along\s+with)\b", bridge, re.I))
            if connectors:
                prefix = bridge[connectors[-1].end():]
        prefix = re.sub(
            r"^\s*(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)*"
            r"(?:(?:only|just)\s+)?"
            r"(?:send|text|message|e-?mail|share|forward|draft|compose|write|schedule)\b",
            "", prefix, count=1, flags=re.I).strip()
        recipient = extract_recipient(value)
        if recipient:
            prefix = re.sub(
                rf"^(?:an?\s+)?(?:message|text|e-?mail)\s+(?:to\s+)?"
                rf"(?:my\s+)?{re.escape(recipient)}\b",
                "", prefix, count=1, flags=re.I).strip()
            prefix = re.sub(
                rf"^(?:to|with)?\s*(?:my\s+)?{re.escape(recipient)}\b",
                "", prefix, count=1, flags=re.I).strip()
        prefix = re.sub(
            r"^(?:an?\s+)?(?:message|text|e-?mail|update)\s+(?:with|about)\b",
            "", prefix, count=1, flags=re.I).strip()
        prefix = re.sub(r"^with\b", "", prefix, count=1, flags=re.I).strip()
        if prefix.lower() not in {"", "my", "the", "all"}:
            pre = re.fullmatch(
                r"(?:(?:my|the|all)\s+)?"
                r"(?P<scope>today|tomorrow)(?:'s|’s)",
                prefix, re.I)
            if not pre:
                return None
            scope = pre.group("scope").lower()

    tail = value[end:]
    if sentence_span and sentence_span[0] >= end:
        tail = tail[:sentence_span[0] - end]
    following = [item for item in mentions if item[0] >= end]
    if following:
        boundary = following[0][0] - end
        bridge = tail[:boundary]
        if connector := re.search(
                r"\b(?:and|plus|with|along\s+with)\b", bridge, re.I):
            tail = bridge[:connector.start()]
        else:
            tail = bridge
    tail = re.sub(
        r"\b(?:via|through|using|by|as)\s+(?:an?\s+)?(?:apple\s+)?"
        r"(?:messages?|texts?|imessage|sms|e-?mail|mail)\b",
        " ", tail, flags=re.I)
    recipient = extract_recipient(value)
    if recipient:
        tail = re.sub(
            rf"\bto\s+(?:my\s+)?{re.escape(recipient)}\b",
            " ", tail, count=1, flags=re.I)
    tail = " ".join(re.sub(r"[,.!?();:]", " ", tail).split())
    while tail:
        previous = tail
        if match := re.match(
                r"^(?:summary|summaries|digest|report|recap|brief|briefing|"
                r"list|schedule|information|part|section)\b", tail, re.I):
            tail = tail[match.end():].strip()
        elif match := re.match(
                r"^(?:(?:due|for|on)\s+)?(?P<scope>today|tomorrow)\b",
                tail, re.I):
            requested = match.group("scope").lower()
            if scope != "all" and scope != requested:
                return None
            scope = requested
            tail = tail[match.end():].strip()
        elif query and (match := re.match(
                r"^about\s+when\s+(?:it\s+is|they\s+are)(?:\s+due)?\b",
                tail, re.I)):
            tail = tail[match.end():].strip()
        elif match := re.match(r"^(?:please|now|immediately)\b", tail, re.I):
            tail = tail[match.end():].strip()
        if tail == previous:
            return None
    if sentence_scope:
        if scope not in {"all", sentence_scope}:
            return None
        scope = sentence_scope
    args = {"query": query, "scope": scope}
    if restriction_args is None:
        return args
    if args["scope"] not in {"all", restriction_args["scope"]}:
        return None
    if args["query"] and restriction_args["query"] not in {"", args["query"]}:
        return None
    merged = {
        "query": restriction_args["query"] or args["query"],
        "scope": restriction_args["scope"],
    }
    if sentence_scope:
        if merged["scope"] not in {"all", sentence_scope}:
            return None
        merged["scope"] = sentence_scope
    return merged


def _source_args(source: str, text: str, date_range: str) -> dict:
    if source == "calendar":
        if re.search(r"\bmove[ -]in\s+date\b", text, re.I):
            return {"days": 60, "calendar_only": True, "query": "move in"}
        if re.search(r"\bthis\s+week\s+and\s+next\s+week\b", text, re.I):
            return {"period": "this week and next week"}
        if date_range in {"today", "tomorrow", "yesterday", "this week", "next week",
                          "last week", "this month", "last month", "next month"}:
            return {"period": date_range}
        days = 7
        lower = text.lower()
        if re.search(r"\b(?:this\s+week\s+and\s+next\s+week|next\s+(?:two|2)\s+weeks?)\b", lower):
            days = 14
        elif re.search(r"\b(?:next|this)\s+month\b", lower):
            days = 31
        elif re.search(r"\b(?:next|this|past|last)\s+(?:few|three|3)\s+weeks?\b", lower):
            days = 21
        elif date_range in {"today"}:
            days = 1
        elif date_range == "tomorrow":
            days = 2
        elif match := re.search(r"(?:past|last|next)\s+(\d+)\s+days?", date_range):
            days = min(60, max(1, int(match.group(1))))
        return {"days": days}
    if source == "reminder":
        return _reminder_source_args(text) or {}
    if source in {"email", "messages"}:
        return _private_source_args(source, text, date_range) or {}
    if source == "stock":
        # Preserve either explicit tickers or the company-name phrase the user
        # supplied; get_stock_price resolves both forms.
        symbols = extract_stock_symbols(text)
        spans = re.findall(
            r"\b(?:exactly\s+)?(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|"
            r"\d+)\s+(?:days?|weeks?|months?)\s+ago|(?:last|past|over\s+the)\s+"
            r"(?:few\s+)?(?:days?|weeks?|months?|year))\b", text, re.I)
        period = spans[-1] if spans else date_range
        return {"symbols": list(dict.fromkeys(symbols)), **({"period": period} if period else {})}
    if source == "news":
        topic = "stock market news" if re.search(r"\b(?:stocks?|market)\b", text, re.I) else "world news"
        return {"query": topic + " today"}
    if source == "daily_brief":
        return {"days": 2 if date_range == "tomorrow" else 1}
    if source == "weather":
        return {"location": extract_location(text), **({"period": date_range} if date_range else {})}
    return {}


def _delivery_scope_text(text: str) -> str:
    """Leading command emphasis does not narrow the report's data source."""
    return re.sub(
        r"^(\s*(?:(?:please|can you|could you|would you)\s+)*)"
        r"(?:only|just)\s+(?=(?:send|text|message|e-?mail|share|forward|draft|compose|write)\s+\S)",
        r"\1", text, flags=re.I)


def _unquoted_scope_text(text: str) -> str:
    """Literal bodies are opaque to source selection, including apostrophes."""
    return _QUOTED_SPAN.sub(' ', text)


def _message_scope_text(text: str) -> str:
    """Preserve quoted qualifiers attached to an explicit private source.

    A complete quoted body is ordinary message text, not a request to read
    private data. Once a caller names email or Messages outside quotes,
    however, removing an attached quoted qualifier can widen a source read.
    Preserve supported Messages date values and leave every other quoted
    qualifier as a marker that must be consumed or rejected before any read.
    """
    unquoted = _unquoted_scope_text(text)
    private_sources = set(extract_sources(unquoted))
    messages_source = "messages" in private_sources
    email_source = "email" in private_sources
    reminder_source = "reminder" in private_sources
    if not (messages_source or email_source or reminder_source):
        return unquoted

    def quoted_scope(match: re.Match[str]) -> str:
        value = match.group(0)
        if value[0] in {'"', "'", '“', '‘'}:
            value = value[1:-1]
        value = value.strip()
        if reminder_source:
            if re.fullmatch(
                    r"(?:due\s+)?(?:today|tomorrow)(?:'s|’s)?", value, re.I):
                return f" {value} "
            return " __UNSUPPORTED_QUOTED_REMINDER_SCOPE__ "
        if messages_source and any(
                pattern.fullmatch(value) for pattern in _RANGE_PATTERNS):
            return f" {value} "
        return " __UNSUPPORTED_QUOTED_PRIVATE_SCOPE__ "

    return _QUOTED_SPAN.sub(quoted_scope, text)


_EMAIL_ACCOUNT = re.compile(
    r"\b(?:from|in|using|for)\s+(?:my\s+)?"
    r"(?P<account>[A-Za-z0-9][A-Za-z0-9 .&'_-]{0,50}?)\s+"
    r"(?:e-?mail\s+)?account\b", re.I)
_MESSAGE_CONVERSATIONS = (
    re.compile(
        r"\b(?:my|the|all)?\s*(?:messages?|texts?)\s+(?:from\s+)?"
        r"(?:my\s+)?(?:conversation|chat)\s+with\s+"
        r"(?P<conversation>[A-Za-z0-9][A-Za-z0-9 .&'_-]{0,60}?)"
        r"(?=\s+(?:to|via|through|using|for|from|on|during|today|yesterday|"
        r"this|last|past|next)\b|[,.!?]|$)", re.I),
    re.compile(
        r"\b(?:my|the|all)?\s*(?:messages?|texts?)\s+with\s+"
        r"(?P<conversation>[A-Za-z0-9][A-Za-z0-9 .&'_-]{0,60}?)"
        r"(?=\s+(?:to|via|through|using|for|from|on|during|today|yesterday|"
        r"this|last|past|next)\b|[,.!?]|$)", re.I),
    re.compile(
        r"\b(?:my|the|all)?\s*(?:messages?|texts?)\s+(?:in|from)\s+(?:the\s+)?"
        r"(?P<conversation>[A-Za-z0-9][A-Za-z0-9 .&'_-]{0,60}?)\s+"
        r"(?:conversation|chat)\b"
        r"(?=\s+(?:to|via|through|using|for|from|on|during|today|yesterday|"
        r"this|last|past|next)\b|[,.!?]|$)", re.I),
)
_EXPLICIT_MESSAGE_CONVERSATIONS = (
    re.compile(
        r"\bwith\s+(?:(?:a|an|the|my|our)\s+)?(?:conversation|chat)"
        r"(?:\s+named)?\s+"
        r"(?P<conversation>[A-Za-z0-9][A-Za-z0-9 .&'_-]{0,60}?)"
        r"(?=\s+(?:to|via|through|using|for|from|on|during|today|yesterday|"
        r"this|last|past|next)\b|[,.!?]|$)", re.I),
)
_DETACHED_MESSAGE_CONVERSATION = re.compile(
    r"\bwith\s+"
    r"(?P<conversation>[A-Za-z0-9][A-Za-z0-9 .&'_-]{0,60}?)"
    r"(?=\s+(?:to|via|through|using|for|from|on|during|today|yesterday|"
    r"this|last|past|next)\b|[,.!?]|$)", re.I)


def _explicit_named_message_conversation(
        text: str) -> tuple[str, tuple[int, int] | None]:
    """Bind an explicit named value before inspecting its internal words."""
    for pattern in _EXPLICIT_MESSAGE_CONVERSATIONS:
        for match in pattern.finditer(text):
            introducer = text[match.start():match.start("conversation")]
            if not re.search(r"\bnamed\s+$", introducer, re.I):
                continue
            return " ".join(match.group("conversation").split()), match.span()
    return "", None


_SOURCE_EXPRESSION_NOUNS = (
    ("daily_brief", r"daily\s+(?:summary|brief|digest)"),
    ("calendar", r"calendar|schedule|agenda"),
    ("reminder", r"reminders?"),
    ("email", r"e-?mails?|mail|inbox"),
    ("messages", r"messages?|texts?"),
    ("stock", r"stocks?|shares?|portfolio"),
    ("news", r"news|headlines?"),
    ("weather", r"weather|forecast"),
)
@dataclass(frozen=True)
class SourceExpressionParse:
    source: str
    consumed_span: tuple[int, int]
    residue: str
    ownership: str

    @property
    def complete(self) -> bool:
        return bool(self.source and not self.residue)


_SOURCE_EXPRESSION_SECTION = re.compile(
    r"^(?:summary|summaries|digest|report|recap|brief|briefing|forecast|list|schedule|"
    r"information|part|section)\b", re.I)
_SOURCE_EXPRESSION_RANGE = re.compile(
    r"^(?:(?:for|from|during|on)\s+)?(?:today|tomorrow|yesterday|"
    r"this\s+week(?:\s+and\s+next\s+week)?|next\s+week|last\s+week|"
    r"this\s+month|next\s+month|last\s+month|"
    r"(?:past|last|next)\s+(?:few|two|2|three|3|\d+)\s+weeks?|"
    r"(?:past|last|next)\s+\d+\s+days?)$", re.I)


def _supported_source_expression(
        source: str, candidate: str, match: re.Match) -> bool:
    """Whether one source grammar consumes the complete candidate."""
    prefix_words = match.group("prefix").split()
    if prefix_words and prefix_words[0].lower() in {"only", "just"}:
        prefix_words = prefix_words[1:]
    if prefix_words and prefix_words[0].lower() in {
            "a", "an", "the", "my", "our", "all"}:
        prefix_words = prefix_words[1:]
    if source == "email" and prefix_words[:1] == ["unread"]:
        prefix_words = prefix_words[1:]
    elif source == "reminder" and re.fullmatch(
            r"(?:today|tomorrow)(?:'s|’s)", " ".join(prefix_words), re.I):
        prefix_words = []
    elif source == "messages" and re.fullmatch(
            r"(?:today|yesterday|tomorrow|(?:last|this|next|past)\s+"
            r"(?:day|week|month|year))(?:'s|’s)",
            " ".join(prefix_words), re.I):
        prefix_words = []
    elif source == "stock" and prefix_words and all(
            re.fullmatch(r"[A-Z][A-Za-z0-9.&'-]*", word)
            for word in prefix_words):
        prefix_words = []
    elif source == "stock" and extract_stock_symbols(candidate):
        prefix_words = []
    if prefix_words:
        return False

    tail = candidate[match.end("noun"):].strip()
    if source == "stock" and extract_stock_symbols(candidate):
        return True
    if source == "stock" and re.fullmatch(
            r"(?:portfolio\s+)?(?:price\s+updates?|updates?|prices?|movements?)?"
            r"(?:\s+(?:(?:for|from|over)\s+)?(?:today|tomorrow|yesterday|"
            r"this\s+week|next\s+week|last\s+week|this\s+month|"
            r"next\s+month|last\s+month))?",
            tail, re.I):
        return True
    if re.fullmatch(
            r"(?:section|part)\s+of\s+(?:my|the)\s+daily\s+"
            r"(?:summary|brief)", tail, re.I):
        return True
    while tail:
        section = _SOURCE_EXPRESSION_SECTION.match(tail)
        if not section:
            break
        tail = tail[section.end():].strip()
    if not tail:
        return True
    if source == "calendar":
        return bool(_SOURCE_EXPRESSION_RANGE.fullmatch(tail))
    if source == "reminder":
        return bool(re.fullmatch(
            r"(?:(?:due|for|on)\s+)?(?:today|tomorrow)", tail, re.I))
    if source == "email":
        return bool(
            _SOURCE_EXPRESSION_RANGE.fullmatch(tail)
            or re.fullmatch(
            r"(?:marked\s+(?:as\s+)?unread|"
            r"(?:that|which)\s+(?:is|are|was|were)\s+unread|"
            r"(?:that\s+)?i\s+(?:haven't|have\s+not)\s+read)"
            r"(?:\s+(?:from|in|using|for)\s+(?:my\s+)?[A-Za-z0-9 .&'_-]+"
            r"\s+(?:e-?mail\s+)?account)?|"
            r"(?:from|in|using|for)\s+(?:my\s+)?[A-Za-z0-9 .&'_-]+\s+"
            r"(?:e-?mail\s+)?account",
            tail, re.I))
    if source == "messages":
        return bool(
            _SOURCE_EXPRESSION_RANGE.fullmatch(tail)
            or re.fullmatch(
                r"with\s+(?:(?:a|an|the|my|our)\s+)?(?:conversation|chat)"
                r"(?:\s+named)?\s+.+|"
                r"with\s+[A-Za-z0-9][A-Za-z0-9 .&'_-]{0,60}|"
                r"(?:in|from)\s+(?:the\s+)?[A-Za-z0-9][A-Za-z0-9 .&'_-]{0,60}"
                r"\s+(?:conversation|chat)",
                tail, re.I))
    if source == "weather":
        location = re.fullmatch(r"(?:in|for)\s+(?P<location>.+)", tail, re.I)
        if not location:
            return False
        words = location.group("location").split()
        if (words and words[0].lower() in {
                "a", "an", "the", "my", "our", "its", "their", "email",
                "message"}
                and not any(word[:1].isupper() for word in words[1:])):
            return False
        return True
    return False


def _parse_source_expression(
        value: str, *, connector: str = "", message_owner: bool = False,
        private_owner: bool = False) -> SourceExpressionParse:
    """Parse one complete candidate without discarding unsupported residue."""
    candidate = value.strip(" \t\r\n,;:()")
    matches: list[tuple[int, int, str, re.Match]] = []
    for source, noun in _SOURCE_EXPRESSION_NOUNS:
        match = re.match(
            rf"^(?P<prefix>(?:[A-Za-z0-9][\w'-]*\s+){{0,4}}?)"
            rf"(?P<noun>{noun})\b",
            candidate, re.I)
        if match:
            matches.append((match.start("noun"), -match.end("noun"), source, match))
    if not matches:
        return SourceExpressionParse("", (0, 0), "", "none")
    unused_start, unused_end, source, match = min(matches)
    prefix_words = match.group("prefix").split()
    validated_stock_expression = (
        source == "stock" and bool(extract_stock_symbols(candidate)))
    if (not validated_stock_expression
            and any(word.lower() in {"and", "plus", "with", "along"}
                    for word in prefix_words)):
        return SourceExpressionParse("", (0, 0), "", "none")
    if (not validated_stock_expression
            and any(word.lower() in {
                "about", "for", "from", "in", "of", "on", "to"}
                    for word in prefix_words)):
        # A noun reached through a preposition is an object in the existing
        # clause, not the head of an independently coordinated source.
        return SourceExpressionParse("", (0, 0), "", "none")
    if (message_owner and connector.lower() == "with" and prefix_words
            and any(word[:1].isupper() for word in prefix_words)
            and not all(word.lower() in {
                "a", "an", "the", "my", "our", "all", "unread", "marked",
            } for word in prefix_words)):
        return SourceExpressionParse("", (0, len(candidate)), "", "conversation")
    supported = _supported_source_expression(source, candidate, match)
    source_tail = candidate[match.end("noun"):].strip()
    if supported and private_owner and source == "weather" and source_tail:
        remaining = source_tail
        while section := _SOURCE_EXPRESSION_SECTION.match(remaining):
            remaining = remaining[section.end():].strip()
        location = re.fullmatch(r"(?:in|for)\s+(.+)", remaining, re.I)
        proper_location = bool(
            location and (
                any(word[:1].isupper() for word in location.group(1).split())
                or re.fullmatch(r"\d{5}(?:-\d{4})?", location.group(1))
                or location.group(1).lower() == "here"))
        if remaining and not (
                _SOURCE_EXPRESSION_RANGE.fullmatch(remaining)
                or proper_location):
            supported = False
    if supported:
        return SourceExpressionParse(
            source, (0, len(candidate)), "", "source")
    residue = " ".join(
        part for part in (
            match.group("prefix").strip(),
            candidate[match.end("noun"):].strip()) if part)
    return SourceExpressionParse(
        source, match.span("noun"), residue or candidate,
        "qualifier" if private_owner else "ambiguous")


def _bare_source_expression(parsed: SourceExpressionParse, candidate: str) -> bool:
    if not parsed.complete:
        return False
    noun = dict(_SOURCE_EXPRESSION_NOUNS).get(parsed.source, r"(?!)")
    return bool(re.fullmatch(rf"(?:{noun})", candidate.strip(), re.I))


def _private_source_owner_before(text: str, end: int) -> str:
    """Return the last payload-private source, excluding delivery syntax."""
    envelopes = _delivery_envelope_spans(text)
    matches = [match for match in re.finditer(
        r"\b(?:e-?mails?|mail|inbox|messages?|texts?)\b", text[:end], re.I)
        if not any(begin <= match.start() < finish
                   for begin, finish in envelopes)]
    if not matches:
        return ""
    return ("email" if re.fullmatch(
        r"e-?mails?|mail|inbox", matches[-1].group(0), re.I)
        else "messages")


def _source_connector_boundary(value: str) -> re.Match | None:
    """Return the last connector whose suffix is a source-shaped clause."""
    for connector in reversed(list(_SOURCE_CONNECTOR.finditer(value))):
        parsed = _parse_source_expression(
            value[connector.end():], connector=connector.group(0),
            message_owner=True, private_owner=True)
        if parsed.source:
            return connector
    return None


def _email_unread(text: str) -> bool:
    return bool(re.search(
        r"\bunread\s+(?:e-?mails?|mail)\b|"
        r"\b(?:e-?mails?|mail)\s+marked\s+(?:as\s+)?unread\b|"
        r"\b(?:e-?mails?|mail)\s+(?:that\s+)?(?:i\s+)?"
        r"(?:haven't|have\s+not)\s+read\b", text, re.I))


def _email_account(text: str) -> str:
    match = _EMAIL_ACCOUNT.search(text)
    return " ".join(match.group("account").split()) if match else ""


def _explicit_message_conversation(text: str) -> str:
    for pattern in _EXPLICIT_MESSAGE_CONVERSATIONS:
        if match := pattern.search(text):
            return " ".join(match.group("conversation").split())
    return ""


def _message_conversation_binding(text: str) -> tuple[str, tuple[int, int] | None]:
    def bound_value(match: re.Match) -> tuple[str, tuple[int, int]]:
        conversation = " ".join(match.group("conversation").split())
        following_envelopes = [start for start, unused_end
                               in _delivery_envelope_spans(text)
                               if start > match.start("conversation")]
        scope_end = min(following_envelopes) if following_envelopes else len(text)
        if punctuation := re.search(r"[.!?]", text[match.start("conversation"):scope_end]):
            scope_end = match.start("conversation") + punctuation.start()
        complete_scope = text[match.start("conversation"):scope_end].strip(
            " \t\r\n,;:()")
        if connector := _source_connector_boundary(complete_scope):
            name = complete_scope[:connector.start()].strip()
            if name:
                end = match.start("conversation") + connector.start()
                return name, (match.start(), end)
        if re.search(r"\b(?:conversation|chat)$", complete_scope, re.I):
            return conversation, match.span()
        parsed_scope = _parse_source_expression(
            complete_scope, connector="with", message_owner=True,
            private_owner=True)
        if parsed_scope.source and not parsed_scope.complete:
            return complete_scope, (match.start(), scope_end)
        return conversation, match.span()

    def valid_name(conversation: str) -> bool:
        if re.search(r"\b(?:conversation|chat)$", conversation, re.I):
            return True
        parsed = _parse_source_expression(
            conversation, connector="with", message_owner=True,
            private_owner=True)
        return not (
            parsed.source and not parsed.complete
            or
            re.match(
                r"(?:(?:a|an|the|my|our)\s+)?"
                r"(?:subjects?|words?|labels?|tags?|senders?)\b",
                conversation, re.I)
            or re.search(
                r"\b(?:subjects?|words?|labels?|tags?|senders?)$",
                conversation, re.I)
            or _is_independent_source_phrase(conversation))

    named_conversation = _explicit_named_message_conversation(text)
    if named_conversation[1]:
        return named_conversation
    for pattern in _EXPLICIT_MESSAGE_CONVERSATIONS:
        if match := pattern.search(text):
            return bound_value(match)
    recipient = extract_recipient(text)
    coordinates = _source_coordinate_spans(text)
    for start in re.finditer(r"\bwith\b", text, re.I):
        if re.search(r"\balong\s+$", text[:start.start()], re.I):
            continue
        match = _DETACHED_MESSAGE_CONVERSATION.match(text, start.start())
        if not match:
            continue
        prior_messages = list(re.finditer(
            r"\b(?:messages?|texts?)\b", text[:start.start()], re.I))
        if (prior_messages
                and re.search(r"\bfrom\b", text[prior_messages[-1].end():start.start()],
                              re.I)):
            continue
        conversation, binding_span = bound_value(match)
        if (recipient and conversation.casefold() == recipient.casefold()
                or not valid_name(conversation)
                or any(begin >= binding_span[0]
                       and binding_span[0] < end and binding_span[1] > begin
                       for begin, end in coordinates)):
            continue
        return conversation, binding_span
    for pattern in _MESSAGE_CONVERSATIONS:
        if match := pattern.search(text):
            conversation, binding_span = bound_value(match)
            if not valid_name(conversation):
                continue
            if re.fullmatch(
                    r"(?:(?:my|the|all)\s+)?(?:calendar|schedule|agenda|"
                    r"e-?mails?|mail|inbox|messages?|texts?|reminders?|weather|"
                    r"forecast|news|headlines?|stocks?|shares?|portfolio)",
                    conversation, re.I):
                continue
            return conversation, binding_span
    return "", None


def _message_conversation(text: str) -> str:
    return _message_conversation_binding(text)[0]


def _mask_message_conversation_binding(text: str) -> str:
    """Hide owned conversation words from draft/schedule intent detection."""
    unused_conversation, span = _message_conversation_binding(text)
    if not span:
        return text
    chars = list(text)
    for index in range(span[0], span[1]):
        chars[index] = " "
    return "".join(chars)


def _private_source_clause(source: str, text: str, date_range: str) -> tuple[list[str], bool, str] | None:
    """Return pre-modifiers, temporal possessive, and the complete source tail."""
    value = " ".join(_unquoted_scope_text(text).split())
    noun = (r"e-?mails?|mail|inbox" if source == "email"
            else r"messages?|texts?")

    def envelope_residual(prefix: str) -> str:
        residual = prefix.strip()
        residual = re.sub(
            r"^(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)*"
            r"(?:(?:only|just)\s+)?"
            r"(?:send|text|message|e-?mail|share|forward|draft|compose|write|"
            r"schedule)\b",
            "", residual, count=1, flags=re.I).strip()
        residual = re.sub(
            r"^what(?:'s|\s+is)\s+(?:on|in)\b",
            "", residual, count=1, flags=re.I).strip()
        recipient = extract_recipient(text)
        if recipient:
            residual = re.sub(
                rf"^(?:an?\s+)?(?:message|text|e-?mail)\s+(?:to\s+)?"
                rf"(?:my\s+)?{re.escape(recipient)}\b",
                "", residual, count=1, flags=re.I).strip()
            residual = re.sub(
                rf"^(?:to\s+)?(?:my\s+)?{re.escape(recipient)}\b",
                "", residual, count=1, flags=re.I).strip()
        if when := _WHEN.match(residual):
            residual = residual[when.end():].strip()
        residual = re.sub(
            r"^(?:(?:only|just)(?:\s+|$))+", "", residual, flags=re.I).strip()
        residual = re.sub(
            r"^(?:with\s+)?(?:an?\s+)?"
            r"(?:summary|digest|report|recap|brief|briefing)\s+"
            r"(?:of|from)\s*$",
            "", residual, count=1, flags=re.I).strip()
        residual = re.sub(r"^with$", "", residual, count=1, flags=re.I).strip()
        residual = re.sub(
            r"^(?:(?:(?:my|the|all)\s+)?(?:calendar|schedule|agenda|messages?|"
            r"texts?|e-?mails?|mail|inbox|reminders?|weather|forecast|news|"
            r"headlines?|stocks?|shares?|portfolio|daily\s+(?:summary|brief|digest))"
            r"(?:\s+(?:summary|summaries|digest|report|recap|part|section))?"
            r"\s+(?:and|with|along\s+with)(?:\s+|$))+",
            "", residual, flags=re.I).strip()
        return residual

    for match in re.finditer(rf"\b(?:{noun})\b", value, re.I):
        prefix = value[:match.start()]
        matched_noun = match.group(0).lower()
        singular = matched_noun in {"email", "e-mail", "message", "text"}
        command_verb = re.fullmatch(
            r"(?:(?:please|can\s+you|could\s+you|would\s+you|only|just)\s*)*",
            prefix, re.I)
        command_object = (
            re.search(
                r"\b(?:send|text|message|e-?mail|share|forward|draft|compose|write|"
                r"schedule)\b",
                prefix, re.I)
            and re.search(r"\b(?:a|an)\s+$", prefix, re.I)
        )
        if singular and (command_verb or command_object):
            continue
        if (source == "email" and matched_noun in {"email", "e-mail"}
                and re.search(
                    r"(?:send|draft|compose|write|schedule)\s+(?:an?\s+)?$",
                    prefix, re.I)
                and not re.match(
                    r"\s+(?:summary|summaries|digest|report|recap|part|section)\b",
                    value[match.end():], re.I)):
            continue
        if (source == "messages" and re.match(
                r"\s+update\b", value[match.end():], re.I)):
            continue
        if re.search(r"(?:via|through|using|to)\s+(?:my\s+)?$", prefix, re.I):
            continue
        determiners = list(re.finditer(r"\b(?:my|the|all)\s+", prefix, re.I))
        determiner = determiners[-1] if determiners else None
        temporal = bool(date_range and re.search(
            rf"{re.escape(date_range)}(?:'s|’s)\s+$", prefix, re.I))
        if determiner:
            head = envelope_residual(prefix[:determiner.start()])
            between = prefix[determiner.end():].strip().lower()
            between = re.sub(
                r"^(?:(?:calendar|schedule|agenda|messages?|texts?|e-?mails?|mail|"
                r"inbox|reminders?|weather|forecast|news|headlines?|stocks?|shares?|"
                r"portfolio|daily\s+(?:summary|brief|digest))"
                r"(?:\s+(?:summary|summaries|digest|report|recap|part|section))?"
                r"\s+(?:and|with|along\s+with)(?:\s+|$))+",
                "", between, flags=re.I).strip()
            words = between.split()
            if head:
                pre = ["__unconsumed_prefix__"]
            elif not words:
                pre = []
            elif len(words) <= 3 and all(re.fullmatch(r"[a-z][\w'-]*", word)
                                         for word in words):
                pre = words
            else:
                pre = ["__unconsumed_prefix__"]
        else:
            residual = envelope_residual(prefix)
            if temporal:
                residual = re.sub(
                    rf"^{re.escape(date_range)}(?:'s|’s)$",
                    "", residual, flags=re.I).strip()
            if source == "email" and residual.lower() == "unread":
                pre = ["unread"]
            elif residual:
                pre = ["__unconsumed_prefix__"]
            else:
                pre = []

        tail = value[match.end():]

        # Parse the complete request rather than treating delivery punctuation
        # as the end of the source phrase. Only exact delivery-envelope and
        # coordinated-source slots are removed; every other word remains for
        # the allowlisted grammar below to consume or reject.
        tail = re.sub(
            r"\b(?:via|through|using|by|as)\s+(?:an?\s+)?(?:apple\s+)?"
            r"(?:messages?|texts?|imessage|sms|e-?mail|mail)\b",
            " ", tail, flags=re.I)
        tail = re.sub(
            r"\bto\s+my\s+(?:e-?mail|inbox)\b",
            " ", tail, flags=re.I)
        recipient = extract_recipient(text)
        if recipient:
            tail = re.sub(
                rf"\bto\s+(?:my\s+)?{re.escape(recipient)}\b",
                " ", tail, count=1, flags=re.I)
        tail = re.sub(
            r"\b(?:and\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
            r"(?:send|text|message|e-?mail|share|forward|draft|compose|write)\s+"
            r"(?:it|them|this|that)\b",
            " ", tail, flags=re.I)
        tail = _remove_source_coordinates(tail, owner_source=source)
        tail = re.sub(r"[,.!?();:]", " ", tail)
        return pre, temporal, " ".join(tail.split())
    return None


def _private_source_args(source: str, text: str, date_range: str) -> dict | None:
    """Parse an entire private-source phrase; residual words fail closed."""
    # These spans are filters the selected tools cannot enforce.  Detect them
    # before the Messages conversation grammar can reinterpret, for example,
    # "with the word calendar" as a conversation name.
    conversation = _message_conversation(text) if source == "messages" else ""
    if (source == "messages" and _private_qualifier_spans(text)
            and not conversation):
        return None
    clause = _private_source_clause(source, text, date_range)
    if clause is None:
        return None
    pre, temporal, remaining = clause
    unread = source == "email" and pre == ["unread"]
    if pre not in ([], ["unread"] if source == "email" else []):
        return None
    account = _email_account(text) if source == "email" else ""

    while remaining:
        previous = remaining
        if match := re.match(
                r"^(?:summary|summaries|digest|report|recap|brief|briefing|part|section)\b",
                remaining, re.I):
            remaining = remaining[match.end():].strip()
        elif match := re.match(
                r"^of\s+(?:my\s+)?daily\s+(?:summary|digest|recap|brief|briefing)\b",
                remaining, re.I):
            remaining = remaining[match.end():].strip()
        elif source == "email" and (match := re.match(
                r"^(?:marked\s+(?:as\s+)?unread|(?:that|which)\s+(?:is|are|was|were)\s+unread|"
                r"(?:that\s+)?i\s+(?:haven't|have\s+not)\s+read)\b",
                remaining, re.I)):
            unread = True
            remaining = remaining[match.end():].strip()
        elif source == "email" and account and (match := re.match(
                rf"^(?:from|in|using|for)\s+(?:my\s+)?{re.escape(account)}\s+"
                r"(?:e-?mail\s+)?account\b", remaining, re.I)):
            remaining = remaining[match.end():].strip()
        elif source == "messages" and conversation and (match := re.match(
                rf"^(?:with\s+(?:(?:a|an|the|my|our)\s+)?(?:conversation|chat)"
                rf"(?:\s+named)?\s+{re.escape(conversation)}|"
                rf"with\s+{re.escape(conversation)}|"
                rf"from\s+(?:my\s+)?(?:conversation|chat)\s+with\s+{re.escape(conversation)}|"
                rf"(?:in|from)\s+(?:the\s+)?{re.escape(conversation)}\s+"
                r"(?:conversation|chat))\b", remaining, re.I)):
            remaining = remaining[match.end():].strip()
        elif date_range and (match := re.match(
                rf"^(?:(?:for|from|during|on)\s+)?{re.escape(date_range)}(?:'s|’s)?\b",
                remaining, re.I)):
            remaining = remaining[match.end():].strip()
        elif match := re.match(r"^(?:please|now|immediately)\b", remaining, re.I):
            remaining = remaining[match.end():].strip()
        if remaining == previous:
            return None

    if source == "email" and unread and date_range:
        return None
    args = {}
    if date_range in {"today", "yesterday"}:
        args["day"] = date_range
    elif date_range:
        args["period"] = date_range
    if source == "email":
        if unread:
            args["unread"] = True
        if account:
            args["account"] = account
    elif conversation:
        args["conversation"] = conversation
    return args


def _private_source_candidate(source: str, text: str, date_range: str) -> bool:
    """Recognize a source noun phrase without deciding whether its tail is valid."""
    value = _unquoted_scope_text(text)
    clause = _private_source_clause(source, text, date_range)
    if source == "email":
        return bool(clause and clause[0])
    if source != "messages":
        return False
    if clause and clause[0]:
        return True
    if re.search(
            r"\b(?:summary|summaries|digest|report|recap)\s+of\s+(?:the\s+)?"
            r"(?:messages?|texts?)\b", value, re.I):
        return True
    if re.search(
            r"\b[A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*){0,2}(?:'s|’s)\s+"
            r"(?:messages?|texts?)\b", value, re.I):
        return True
    if re.search(r"\b(?:my|the)\s+(?:message|text)\b", value, re.I):
        return bool(clause and clause[2])
    return False


def _unsupported_message_sender(text: str) -> bool:
    """Compatibility helper backed by the complete allowlisted phrase parser."""
    scoped = _message_scope_text(text)
    if "__UNSUPPORTED_QUOTED_PRIVATE_SCOPE__" in scoped:
        return True
    scoped = _delivery_scope_text(_normalize(scoped))
    if "messages" not in extract_sources(scoped):
        return False
    return _private_source_args("messages", scoped, _date_range(scoped)) is None


def _named_source_sections(text: str) -> list[str]:
    narrowed = []
    for source, noun in (("email", r"e-?mails?|inbox"),
                         ("messages", r"messages?|texts?"),
                         ("calendar", r"calendar|schedule|agenda"),
                         ("weather", r"weather|forecast"),
                         ("reminder", r"reminders?"),
                         ("news", r"news|headlines?"),
                         ("stock", r"stocks?|shares?|portfolio")):
        if re.search(rf"\b(?:only|just)\s+(?:the\s+|my\s+)?(?:{noun})\b|"
                     rf"\b(?:{noun})\s+(?:part|section)\b", text, re.I):
            narrowed.append(source)
    return narrowed


_SOURCE_NOUN = (
    r"calendar|schedule|agenda|e-?mails?|mail|inbox|messages?|texts?|"
    r"reminders?|weather|forecast|news|headlines?|stocks?|shares?|portfolio")
_SOURCE_SECTION = (
    r"summary|summaries|digest|report|recap|brief|briefing|list|schedule|"
    r"information|part|section")
_INDEPENDENT_SOURCE_PHRASE = (
    rf"(?:(?:my|the|all)\s+(?:{_SOURCE_NOUN})"
    rf"(?:\s+(?:{_SOURCE_SECTION}))?|"
    rf"(?:{_SOURCE_NOUN})\s+(?:{_SOURCE_SECTION})|"
    r"unread\s+e-?mails?)")
_SOURCE_CONNECTOR = re.compile(
    r"\b(?:and|plus|with|along\s+with)\b", re.I)
_EMAIL_QUALIFIER_INTRODUCER = re.compile(
    r"(?:with|along\s+with)(?:\s+(?:a|an|the))?\s+"
    r"(?:subjects?|words?|labels?|tags?|senders?)\b|"
    r"(?:with|along\s+with)\b|"
    r"containing\b|matching\b|about\b|whose\b|"
    r"(?:labeled|labelled|tagged)\b|"
    r"(?:subjects?|words?|labels?|tags?|senders?)\b|from\b",
    re.I)
_MESSAGE_QUALIFIER_INTRODUCER = re.compile(
    r"(?:with|along\s+with)(?:\s+(?:a|an|the|my|our))?\s+"
    r"(?:subjects?|words?|labels?|tags?|senders?|conversations?|chats?)\b|"
    r"containing\b|matching\b|about\b|whose\b|"
    r"(?:labeled|labelled|tagged)\b|"
    r"(?:subjects?|words?|labels?|tags?|senders?)\b|"
    rf"(?:with|along\s+with)\s+(?=(?:the\s+)?(?:{_SOURCE_NOUN})\b)",
    re.I)


def _delivery_envelope_spans(text: str) -> list[tuple[int, int]]:
    spans = [match.span() for match in re.finditer(
        r"\b(?:via|through|using|by|as)\s+(?:an?\s+)?(?:apple\s+)?"
        r"(?:messages?|texts?|imessage|sms|e-?mail|mail)\b", text, re.I)]
    recipient = extract_recipient(text)
    if recipient:
        spans.extend(match.span() for match in re.finditer(
            rf"\b(?:to|with)\s+(?:my\s+)?{re.escape(recipient)}\b", text, re.I))
    return spans


def _private_qualifier_spans(text: str) -> list[tuple[int, int]]:
    """Bind filters across the full request while masking delivery syntax."""
    source_nouns = (
        ("email", re.compile(
            r"\b(?:(?:my|the|all)\s+)?(?:e-?mails?|mail|inbox)\b", re.I),
         _EMAIL_QUALIFIER_INTRODUCER),
        ("messages", re.compile(
            r"\b(?:(?:my|the|all)\s+)?(?:messages?|texts?)\b", re.I),
         _MESSAGE_QUALIFIER_INTRODUCER),
    )
    value_first = re.compile(
        r"\b(?:[A-Za-z0-9][\w'-]*\s+){1,8}"
        r"(?:subjects?|words?|labels?|tags?|senders?|conversations?|chats?)\b",
        re.I)
    spans: list[tuple[int, int]] = []
    coordinates = _source_coordinate_spans(text)
    envelope_spans = _delivery_envelope_spans(text)
    conversation, conversation_span = _message_conversation_binding(text)
    for source_kind, noun, introducer in source_nouns:
        for source in noun.finditer(text):
            if (conversation_span
                    and conversation_span[0] < source.start()
                    and source.end() <= conversation_span[1]):
                # A source-like token inside a conversation name belongs to
                # that already-bound qualifier.  It cannot open a second
                # source clause or mask the original Messages source.
                continue
            if any(source.start() < end and source.end() > begin
                   for begin, end in envelope_spans):
                continue
            matched_noun = source.group(0).lower().strip()
            bare_noun = re.sub(r"^(?:my|the|all)\s+", "", matched_noun)
            prefix = text[:source.start()]
            if bare_noun in {"email", "e-mail", "message", "text"}:
                command_object = (
                    re.search(
                        r"\b(?:send|text|message|e-?mail|share|forward|draft|"
                        r"compose|write|schedule)\b", prefix, re.I)
                    and re.search(r"\b(?:a|an)\s+$", prefix, re.I))
                following_summary = re.match(
                    r"\s+(?:summary|summaries|digest|report|recap|part|section)\b",
                    text[source.end():], re.I)
                if (matched_noun == bare_noun and command_object
                        and not following_summary):
                    continue
                if (not prefix.strip()
                        and re.match(r"\s+\S+", text[source.end():])):
                    continue
            if (source_kind == "messages" and conversation and conversation_span
                    and source.end() <= conversation_span[1]):
                qualifier_start = conversation_span[0]
                if qualifier_start <= source.start():
                    relation = re.search(
                        r"\b(?:with|from|in)\b",
                        text[source.end():conversation_span[1]], re.I)
                    if relation:
                        qualifier_start = source.end() + relation.start()
                if not any(
                        begin >= qualifier_start
                        and qualifier_start < end
                        and conversation_span[1] > begin
                        for begin, end in coordinates):
                    spans.append((qualifier_start, conversation_span[1]))
            chars = list(text[source.end():])
            for begin, end in (*coordinates, *envelope_spans):
                if begin <= source.start() < end:
                    continue
                for index in range(max(begin, source.end()) - source.end(),
                                   end - source.end()):
                    if 0 <= index < len(chars):
                        chars[index] = " "
            clause = "".join(chars)
            candidates = list(introducer.finditer(clause))
            candidates.extend(value_first.finditer(clause))
            structural_starts = []
            for connector in _SOURCE_CONNECTOR.finditer(text, source.end()):
                if any(begin <= connector.start() < end
                       for begin, end in (*coordinates, *envelope_spans)):
                    continue
                candidate, unused_end = _source_connector_segment(text, connector)
                parsed = _parse_source_expression(
                    candidate, connector=connector.group(0),
                    message_owner=(source_kind == "messages"),
                    private_owner=True)
                ambiguous_email_value = (
                    source_kind == "email"
                    and connector.group(0).lower() in {"with", "along with"}
                    and _bare_source_expression(parsed, candidate))
                if ((parsed.source and not parsed.complete)
                        or ambiguous_email_value):
                    structural_starts.append(connector.start())
            if source_kind == "messages" and conversation_span:
                candidates = [candidate for candidate in candidates
                              if not (
                                  source.end() + candidate.start()
                                  < conversation_span[1]
                                  or (source.end() + candidate.start()
                                      >= conversation_span[1]
                                      and not text[
                                          conversation_span[1]:
                                          source.end() + candidate.start()
                                      ].strip()))]
                structural_starts = []
            if not candidates and not structural_starts:
                continue
            qualifier_start = min(
                [source.end() + candidate.start() for candidate in candidates]
                + structural_starts)
            ends = [begin for begin, unused_end in coordinates
                    if begin > qualifier_start]
            spans.append((qualifier_start, min(ends) if ends else len(text)))
    return list(dict.fromkeys(spans))


def _source_connector_segment(
        text: str, connector: re.Match) -> tuple[str, int]:
    """Return one complete connector-delimited payload segment."""
    ends = [len(text)]
    ends.extend(begin for begin, unused_end in _delivery_envelope_spans(text)
                if begin >= connector.end())
    limiter = _CONSTRAINT_INTRODUCER.search(text, connector.end())
    if limiter:
        ends.append(limiter.start())
    punctuation = re.search(r"[.!?]", text[connector.end():])
    if punctuation:
        ends.append(connector.end() + punctuation.start())
    outer_end = min(ends)
    unused_named, named_span = _explicit_named_message_conversation(text)
    complete_probe = text[connector.end():outer_end].strip(" \t\r\n,;:()")
    prior = text[:connector.start()]
    complete_parse = _parse_source_expression(
        complete_probe, connector=connector.group(0),
        message_owner=bool(re.search(r"\b(?:messages?|texts?)\b", prior, re.I)),
        private_owner=bool(re.search(
            r"\b(?:e-?mails?|mail|inbox|messages?|texts?)\b", prior, re.I)))
    if complete_parse.complete:
        return complete_probe, outer_end
    for following in _SOURCE_CONNECTOR.finditer(
            text, connector.end(), outer_end):
        if named_span and named_span[0] <= following.start() < named_span[1]:
            continue
        probe = text[following.end():outer_end].strip(" \t\r\n,;:()")
        prior = text[:following.start()]
        parsed = _parse_source_expression(
            probe, connector=following.group(0),
            message_owner=bool(re.search(
                r"\b(?:messages?|texts?)\b", prior, re.I)),
            private_owner=bool(re.search(
                r"\b(?:e-?mails?|mail|inbox|messages?|texts?)\b", prior, re.I)))
        if parsed.source:
            ends.append(following.start())
            break
    end = min(ends)
    return text[connector.end():end].strip(" \t\r\n,;:()"), end


def _source_coordinate_spans(
        text: str, start: int = 0,
        owner_source: str = "") -> list[tuple[int, int]]:
    """Return only proven source boundaries; retain unsupported residue."""
    unused_named, named_span = _explicit_named_message_conversation(text)
    connectors = [match for match in _SOURCE_CONNECTOR.finditer(text)
                  if (match.start() >= start
                      and not (named_span
                               and named_span[0] <= match.start() < named_span[1]))]
    spans: list[tuple[int, int]] = []
    for connector in connectors:
        if any(begin < connector.start() < end for begin, end in spans):
            continue
        candidate, end = _source_connector_segment(text, connector)
        prior_private = list(re.finditer(
            r"\b(?:e-?mails?|mail|inbox|messages?|texts?)\b",
            text[:connector.start()], re.I))
        message_mentions = list(re.finditer(
            r"\b(?:messages?|texts?)\b", text[:connector.start()], re.I))
        message_owner = bool(message_mentions)
        parsed = _parse_source_expression(
            candidate, connector=connector.group(0),
            message_owner=message_owner,
            private_owner=(bool(prior_private)
                           or owner_source in {"email", "messages"}))
        previous_after_message = bool(
            message_mentions and any(
                prior.end() <= earlier.start() < connector.start()
                for prior in message_mentions[-1:]
                for earlier in connectors))
        proven_boundary = (
            parsed.complete
            or connector.group(0).lower() in {"and", "plus"}
            or previous_after_message)
        email_owner = (
            owner_source == "email"
            or _private_source_owner_before(text, connector.start()) == "email")
        ambiguous_email_value = (
            email_owner
            and connector.group(0).lower() in {"with", "along with"}
            and _bare_source_expression(parsed, candidate))
        if parsed.source and proven_boundary and not ambiguous_email_value:
            spans.append((connector.start(), end))
    return spans


def _source_expression_residue(text: str) -> str:
    """Return the first unconsumed source-headed payload segment."""
    unused_named, named_span = _explicit_named_message_conversation(text)
    unused_conversation, conversation_span = _message_conversation_binding(text)
    connectors = [match for match in _SOURCE_CONNECTOR.finditer(text)
                  if not ((named_span
                           and named_span[0] <= match.start() < named_span[1])
                          or (conversation_span
                              and conversation_span[0] <= match.start()
                              < conversation_span[1]))]
    private_noun = re.compile(
        r"\b(?:e-?mails?|mail|inbox|messages?|texts?)\b", re.I)
    for connector in connectors:
        if (not text[:connector.start()].strip()
                and re.match(
                    r"\s*(?:send|text|message|e-?mail|share|forward|draft|"
                    r"compose|write|schedule)\b",
                    text[connector.end():], re.I)):
            continue
        candidate, unused_end = _source_connector_segment(text, connector)
        prior = text[:connector.start()]
        parsed = _parse_source_expression(
            candidate, connector=connector.group(0),
            message_owner=bool(re.search(r"\b(?:messages?|texts?)\b", prior, re.I)),
            private_owner=bool(private_noun.search(prior)))
        email_owner = (
            _private_source_owner_before(text, connector.start()) == "email")
        if (email_owner
                and connector.group(0).lower() in {"with", "along with"}
                and _bare_source_expression(parsed, candidate)):
            return candidate
        if parsed.source and parsed.residue:
            return parsed.residue

    effect = re.match(
        r"^\s*(?:and\s+)?(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)*"
        r"(?:(?:only|just)\s+)?"
        r"(?:send|text|message|e-?mail|share|forward|draft|compose|write|schedule)\b",
        text, re.I)
    start = effect.end() if effect else 0
    ends = []
    for connector in connectors:
        if connector.start() < start:
            continue
        candidate, unused_end = _source_connector_segment(text, connector)
        prior = text[:connector.start()]
        parsed_connector = _parse_source_expression(
            candidate, connector=connector.group(0),
            message_owner=bool(re.search(
                r"\b(?:messages?|texts?)\b", prior, re.I)),
            private_owner=bool(private_noun.search(prior)))
        if parsed_connector.source:
            ends.append(connector.start())
    ends.extend(begin for begin, unused_end in _delivery_envelope_spans(text)
                if begin >= start)
    leading = text[start:min(ends) if ends else len(text)]
    leading = re.sub(
        r"^\s*(?:send|text|message|e-?mail|share|forward|draft|compose|write)\b",
        "", leading, flags=re.I)
    recipient = extract_recipient(text)
    if recipient == "me":
        leading = re.sub(
            r"\s+to\s+my\s+(?:e-?mail|messages?|texts?)\b.*$",
            "", leading, flags=re.I)
    if recipient:
        recipient_pattern = r"\s+".join(
            re.escape(part) for part in recipient.split())
        leading = re.sub(
            rf"^\s*{recipient_pattern}\b", "", leading, flags=re.I)
    else:
        recipient_pattern = r"[A-Za-z0-9@._+\-]+"
    leading = re.sub(
        r"^\s*(?:a|an)\s+(?:messages?|texts?|e-?mails?)\b"
        r"(?:\s+(?:update|summary|report))?"
        rf"(?:\s+to\s+{recipient_pattern})?\b",
        "", leading, flags=re.I)
    parsed = _parse_source_expression(leading)
    return parsed.residue if parsed.source and parsed.residue else ""


def _is_independent_source_phrase(candidate: str) -> bool:
    if re.fullmatch(_INDEPENDENT_SOURCE_PHRASE, candidate, re.I):
        return True
    calendar_range = (
        r"today|tomorrow|yesterday|this\s+week|next\s+week|last\s+week|"
        r"this\s+month|next\s+month|last\s+month|next\s+(?:two|2|three|3)\s+weeks?|"
        r"next\s+few\s+weeks?")
    if re.fullmatch(
            rf"(?:(?:my|the|all)\s+)?(?:calendar|schedule|agenda)"
            rf"(?:\s+(?:section|part|summary|report))?\s+"
            rf"(?:for|on)\s+(?:{calendar_range})", candidate, re.I):
        return True
    return bool(re.fullmatch(
        r"(?:(?:my|the|all)\s+)?reminders?"
        r"(?:\s+(?:section|part|summary|list))?\s+"
        r"(?:due\s+)?(?:today|tomorrow)", candidate, re.I))


def _remove_source_coordinates(text: str, owner_source: str = "") -> str:
    chars = list(text)
    for start, end in _source_coordinate_spans(
            text, owner_source=owner_source):
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _mask_source_qualifiers(text: str) -> str:
    chars = list(text)
    for start, end in _private_qualifier_spans(text):
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _payload_source_mentions(
        text: str, *, qualifiers_masked: bool = False
        ) -> list[tuple[int, int, str]]:
    """Source nouns in payload roles, excluding effects, people and channels."""
    scope = _unquoted_scope_text(_normalize(text))
    excluded: list[tuple[int, int]] = (
        [] if qualifiers_masked else _private_qualifier_spans(scope))

    def exclude(pattern: str, *, group: str | None = None) -> None:
        for match in re.finditer(pattern, scope, re.I):
            excluded.append(match.span(group) if group else match.span())

    exclude(
        r"\b(?:via|through|using|by|as)\s+(?:an?\s+)?(?:apple\s+)?"
        r"(?:messages?|texts?|imessage|sms|e-?mail|mail)\b")
    exclude(r"\b(?:an?\s+)?(?:messages?|texts?)\s+update\b")
    exclude(
        r"^\s*(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)*"
        r"(?:(?:only|just)\s+)?"
        r"(?P<verb>send|text|message|e-?mail|share|forward|draft|compose|write|schedule)\b",
        group="verb")
    exclude(
        r"\b(?:send|draft|compose|write|schedule)\s+(?:an?\s+)?"
        r"(?P<object>e-?mail|message|text)\b", group="object")
    recipient = extract_recipient(scope)
    if recipient:
        exclude(rf"\b(?:to|with)\s+(?:my\s+)?{re.escape(recipient)}\b")

    nouns = (
        ("daily_brief", r"daily\s+(?:summary|brief|digest)|morning\s+brief|full\s+briefing"),
        ("calendar", r"calendar|agenda|schedule"),
        ("reminder", r"reminders?"),
        ("email", r"(?:(?:my|the|all)\s+e-?mail)|e-?mails|mail|inbox|"
                  r"e-?mail(?=\s+(?:summary|summaries|digest|report|recap|part|section)\b)"),
        ("messages", r"(?:(?:my|the|all)\s+(?:message|text))|messages|texts|"
                     r"(?:message|text)(?=\s+(?:summary|summaries|digest|report|recap|part|section)\b)"),
        ("stock", r"stocks?|shares?|portfolio"),
        ("news", r"news|headlines?"),
        ("weather", r"weather|forecast"),
    )

    mentions: list[tuple[int, int, str]] = []
    for source, noun in nouns:
        for match in re.finditer(rf"\b(?:{noun})\b", scope, re.I):
            start, end = match.span()
            if any(start < stop and end > begin for begin, stop in excluded):
                continue
            mentions.append((start, end, source))

    # Prefer the widest lexical role when patterns overlap, then preserve the
    # user's source order. A source repeated later does not create a new slot.
    mentions.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, str]] = []
    for mention in mentions:
        if any(mention[0] < item[1] and mention[1] > item[0] for item in selected):
            continue
        selected.append(mention)
    return selected


def _coordinated_source_mentions(
        text: str, *, qualifiers_masked: bool = False) -> list[str]:
    mentions = _payload_source_mentions(
        text, qualifiers_masked=qualifiers_masked)
    unique = []
    for mention in mentions:
        if mention[2] not in [item[2] for item in unique]:
            unique.append(mention)
    if len(unique) < 2:
        return []
    if not all(re.search(
            r"\b(?:and|plus|with|along\s+with)\b",
            text[left[1]:right[0]], re.I)
            for left, right in zip(unique, unique[1:])):
        return []
    return [mention[2] for mention in unique]


def _source_mentions_are_coordinated(text: str) -> bool:
    return bool(_coordinated_source_mentions(text))


def extract_sources(text: str) -> list[str]:
    text = _mask_source_qualifiers(
        _delivery_scope_text(_unquoted_scope_text(text)))
    for pattern in _RANGE_PATTERNS:
        text = re.sub(rf"(?:{pattern.pattern})(?:'s|’s)\s+(messages|texts)\b",
                      r"my \1", text, flags=re.I)
    sources: list[str] = []
    if re.search(r"\b(?:daily\s+(?:summary|digest|recap|brief|briefing)|morning\s+brief|full\s+briefing)\b", text, re.I):
        sources.append("daily_brief")
    if re.search(
            r"\b(?:from|with|using)\s+(?:my\s+)?(?:apple\s+)?reminders?(?:\.app)?\b|"
            r"\breminders?\s+(?:summary|list|schedule|information)\b", text, re.I):
        sources.append("reminder")
    if re.search(r"\b(?:cal[ae]ndar|agenda|meetings?|events?|appointments?|move[ -]in\s+date|"
                 r"my\s+schedule|schedule\s+(?:summary|report|digest|recap))\b", text, re.I):
        sources.append("calendar")
    # Remove explicit destination phrases before looking for an inbox source;
    # "send a news report to my email" names email as the channel, not data to
    # summarize.
    email_payload = re.sub(
        r"\b(?:to|via|through|using)\s+my\s+(?:e-?mail|inbox)\b", "", text,
        flags=re.I)
    private_payload = re.sub(
        r"\b(?:via|through|using|by|as)\s+(?:an?\s+)?(?:apple\s+)?"
        r"(?:messages?|texts?|imessage|sms|e-?mail|mail)\b",
        " ", text, flags=re.I)
    private_payload = re.sub(
        r"\b(?:an?\s+)?(?:messages?|texts?)\s+update\b",
        " ", private_payload, flags=re.I)
    if (re.search(r"\b(?:my\s+e-?mails?|my\s+inbox|inbox|unread\s+e-?mails?|"
                  r"e-?mails?\s+(?:summary|summaries|digest|report|part|section)|"
                  r"(?:only|just)\s+(?:the\s+)?e-?mails?)\b", email_payload, re.I)
            and (_SUMMARY.search(text) or _OUTBOUND.search(text))):
        sources.append("email")
    elif (re.search(r"\b(?:(?:my|the|all)\s+)?(?:e-?mails|mail|inbox)\b",
                    private_payload, re.I)
          and _OUTBOUND.search(text)):
        sources.append("email")
    elif _private_source_candidate("email", text, _date_range(text)):
        sources.append("email")
    if re.search(r"\b(?:my\s+(?:messages|texts)|"
                 r"(?:messages?|texts?)\s+(?:summary|summaries|digest|report|part|section)|"
                 r"(?:only|just)\s+(?:the\s+)?(?:messages|texts))\b", text, re.I):
        sources.append("messages")
    elif (re.search(r"\b(?:(?:my|the|all)\s+)?(?:messages|texts)\b",
                    private_payload, re.I)
          and _OUTBOUND.search(text)):
        sources.append("messages")
    elif _private_source_candidate("messages", text, _date_range(text)):
        # Candidate recognition identifies the source noun phrase; acceptance
        # is decided later by the complete allowlisted parser.
        sources.append("messages")
    if (re.search(r"\b(?:stocks?|shares?|portfolio|tickers?|market|\$[A-Z]{1,5})\b", text, re.I)
            and _SUMMARY.search(text)):
        sources.append("stock")
    if re.search(r"\b(?:news|headlines?)\b", text, re.I):
        sources.append("news")
    if re.search(r"\b(?:weather|forecast)\b", text, re.I):
        sources.append("weather")
    mentions = _payload_source_mentions(text, qualifiers_masked=True)
    mention_sources = list(dict.fromkeys(mention[2] for mention in mentions))
    narrowed = _named_source_sections(text)
    related_sources = (mention_sources if narrowed else
                       _coordinated_source_mentions(
                           text, qualifiers_masked=True))
    if not related_sources and "reminder" in mention_sources:
        related_sources = ["reminder"]
    for source in related_sources:
        if source not in sources:
            sources.append(source)
    # A named section of a broad report is the payload, not an additional
    # source. Reading daily_brief alongside email would still disclose the
    # calendar and conversations that the user explicitly excluded.
    if narrowed:
        combined = list(dict.fromkeys([*sources, *narrowed]))
        if set(combined) - {"daily_brief"} - set(narrowed):
            # Preserve every independent source mention. The compiler checks
            # below whether the relationship is explicit or needs clarification.
            return combined
        return narrowed
    return list(dict.fromkeys(sources))


def plain_reference_request(text: str) -> bool:
    """Only a fully understood forwarding command can reuse an old payload.

    Do not try to enumerate every possible edit verb: any unconsumed content
    instruction makes the reference unsafe, including new verbs or languages.
    Recipient text is accepted only in a delivery slot, never as free prose.
    """
    text = _normalize(text).strip()
    recipient = extract_recipient(text)
    who = re.escape(recipient) if recipient else r"(?!)"
    prefix = r"(?:(?:please|ok|okay|yes|actually|and|can you|could you|schedule)\s+)*"
    verb = r"(?:send|text|message|e-?mail|share|forward|draft|compose|write)"
    payload = (r"(?:this|that|it|(?:(?:my|the|this|that)\s+)?"
               r"(?:daily|calendar|news|e-?mail|messages?|weather|stock)\s+"
               r"(?:summary|report|digest|brief|briefing|recap))")
    channel = r"(?:via|through|using|by|as)\s+(?:a\s+)?(?:messages?|texts?|imessage|e-?mail)"
    suffix = rf"(?:\s+(?:{channel}|{_WHEN.pattern}|now|immediately|please|only))*[.!?]*"
    direct = rf"{verb}\s+{payload}(?:\s+(?:to\s+)?{who})?"
    addressed = rf"{verb}\s+{who}\s+{payload}"
    composed = rf"{verb}\s+(?:an?\s+)?(?:message|text|e-?mail)\s+to\s+{who}\s+with\s+{payload}"
    # The reported compound read explicitly names new content before 'it'.
    text = re.sub(r"^what(?:'s| is)\s+(?:on|in)\s+my\s+(?:e-?mails?|inbox|messages|texts)"
                  r"\s+and\s+", "", text, flags=re.I)
    return bool(re.fullmatch(rf"{prefix}(?:{direct}|{addressed}|{composed}){suffix}", text, re.I))


def references_content(text: str) -> bool:
    text = _WHEN.sub("", text)
    for pattern in _RANGE_PATTERNS:
        text = pattern.sub("", text)
    # A source query such as vaccine information 'about when it is' refers
    # to that source's event, not the preceding assistant's text.
    text = re.sub(r"\babout when it is\b", "", text, flags=re.I)
    # This relative pronoun belongs to the exact supported unread-email
    # qualifier, not to prior assistant content.
    text = re.sub(
        r"\bthat(?=\s+i\s+(?:haven't|have\s+not)\s+read\b)",
        "", text, flags=re.I)
    return bool(re.search(r"\b(?:it|this|that|above|previous|earlier)\b", text, re.I))


def explicit_delivery(text: str) -> str:
    """A correction inherits mode only when no delivery mode was supplied."""
    if _DRAFT.search(text):
        return "draft"
    if _SCHEDULE.search(text) or _WHEN.search(text):
        return "scheduled"
    if re.search(r"\b(?:send|forward|share|now|immediately)\b|"
                 r"\b(?:text|message|email)\s+(?:it|this|that|to|mom|dad)\b", text, re.I):
        return "send"
    recipient = extract_recipient(text)
    if recipient and re.search(rf"\b(?:text|message|e-?mail)\s+(?:to\s+)?"
                               rf"{re.escape(recipient)}\b", text, re.I):
        return "send"
    return ""


def extract_delivery_time(text: str) -> str:
    match = _WHEN.search(text)
    return match.group(0) if match else ""


def unsupported_summary_modifier(
        text: str, *, validated_stock_symbols: tuple[str, ...] = ()) -> bool:
    """Unknown summary adjectives are content instructions, not decoration.

    Retrieval has no redaction/rewriting contract. Recognize only structural
    words and fresh-source wording; new edit adjectives must not silently
    become an unmodified inbox/report delivery.
    """
    structural = set("my the a an this that your our send text message email emails share forward "
                     "draft compose write with of from and calendar daily weather news "
                     "stock stocks inbox messages reminder reminders recent latest fresh new".split())
    structural.update(extract_recipient(text).lower().split())
    identifiers = {symbol.lower() for symbol in validated_stock_symbols}
    pattern = (r"\b([A-Za-z][\w-]*)\s+"
               r"(?:(?:e-?mail|inbox|calendar|messages?|reminders?|weather|news|stocks?|daily)\s+)?"
               r"(?:summary|summaries|report|digest|brief|recap)\b")
    return any(match.group(1).lower() not in structural | identifiers
               for match in re.finditer(pattern, text, re.I))


def compile_new(text: str, *, last_user: str = "", last_assistant: str = "") -> WorkflowPlan | None:
    original = text
    text = _delivery_scope_text(_normalize(_message_scope_text(text)))
    if REMINDER_CREATE_RE.search(text):
        return None
    if _INLINE_EMAIL_SUMMARY.match(text.strip()):
        return None
    if not _OUTBOUND.search(text):
        return None
    sources = extract_sources(text)
    narrowed_sections = _named_source_sections(text)
    independent_sections = (
        set(sources) - {"daily_brief"} - set(narrowed_sections))
    ambiguous_section_scope = bool(
        narrowed_sections and independent_sections
        and not _source_mentions_are_coordinated(text))
    # Bind a referent before tool retrieval can reinterpret it as an email or
    # note. Named reports can refer to the answer just produced, too.
    refers_back = references_content(text)
    plain_reference = plain_reference_request(text)
    prior_sources = extract_sources(last_user)
    # An unchanged named report can refer to the answer just produced. A new
    # read, range, subset or transformation cannot inherit that answer.
    modified = bool(re.search(
        r"\b(?:only|just|part|section|except|exclude|without|instead|"
        r"shorten|shorter|rewrite|rephrase|translate|translation|summarize|"
        r"summarise|condense|bullet|sentence|paragraph|first|last|latest|fresh|new)\b",
        text, re.I))
    new_read = bool(re.search(r"\b(?:what(?:'s| is)|check|retrieve|fetch|read)\b", text, re.I))
    named_report = bool(sources and sources == prior_sources and _SUMMARY.search(text)
                        and not modified and not new_read
                        and _date_range(text) == _date_range(last_user)
                        and not _OUTBOUND.search(last_user)
                        and last_assistant and not last_assistant.rstrip().endswith('?'))
    # Conversation prose has no source provenance. Even matching prior user
    # requests cannot prove that last_assistant contains that result. Explicit
    # sources always run afresh; unresolved references clarify. Actual tool
    # receipts are bound separately by receipt_notification.
    artifact = ""
    # Unknown transformations are deliberately not approximated by forwarding
    # the whole answer. Keep this inside the workflow so ordinary routing
    # cannot turn an unresolved reference into an outbound effect.
    transform = bool(re.search(
        r"\b(?:shorten|shorter|rewrite|rephrase|translate|translation|condense|"
        r"bullet|sentence|paragraph|except|exclud\w*|without|remov\w*|redact\w*|"
        r"omit\w*|omission|anonym\w*|saniti[sz]\w*|strip\w*|mask\w*|"
        r"de-?identif\w*|conceal\w*|obfuscat\w*|censor\w*|scrub\w*|clean\w*|"
        r"obscur\w*|hidden|hide|hiding)\b|"
        r"\b(?:first|last|top)\s+(?:\d+\s+)?(?:e-?mails?|messages?|items?|entries)\b",
        text, re.I))
    content_error = ""
    unresolved_subset = modified and not sources and bool(re.search(
        r"\b(?:part|section|first|last|only|just)\b", text, re.I))
    unknown_section = bool("daily_brief" in sources and (
        len(sources) > 1 or re.search(r"\b(?:part|section|only|just)\b", text, re.I)))
    date_range = _date_range(text)
    constraint_ledger = _complete_source_constraint_ledger(text)
    constraint_scopes = constraint_ledger.scopes
    constraint_error = not constraint_ledger.complete
    original_sources = tuple(sources)
    if constraint_ledger.selected_sources:
        if not constraint_ledger.selected_sources.issubset(sources):
            constraint_error = True
        else:
            sources = [source for source in sources
                       if source in constraint_ledger.selected_sources]
    segmentation = _request_segmentation(text, constraint_ledger.spans)
    retained_sources = set(sources)
    retained_text = segmentation.retained_text(retained_sources)
    source_ranges, source_range_error = _source_owned_date_ranges(
        segmentation, sources)
    # Preserve the established shared-range grammar for coordinated reports.
    # Reminder dates are different: they are item filters and therefore can
    # never supply a missing range to another source.
    if ("reminder" not in sources and date_range
            and not _has_explicit_owned_date_range(segmentation, source_ranges)):
        source_ranges = {
            source: source_ranges.get(source, "") or date_range
            for source in sources
        }
    validated_stock_symbols = tuple(dict.fromkeys(
        extract_stock_symbols(segmentation.source_text("stock", retained_sources))
        + extract_stock_symbols(text))) if "stock" in sources else ()
    scoped_args = {
        source: (_private_source_args(
                     source,
                     segmentation.retained_text({source}),
                     source_ranges.get(source, ""))
                 if source in {"email", "messages"}
                 else _reminder_source_args(
                     segmentation.retained_text({source})))
        for source in sources if source in {"email", "messages", "reminder"}
    }
    source_scope_error = any(value is None for value in scoped_args.values())
    legacy_message_scope_error = (
        "messages" in sources and _unsupported_message_sender(
            segmentation.retained_text({"messages"})))
    adjacent_residue = _source_adjacent_residue(segmentation, sources)
    source_expression_residue = _source_expression_residue(text)
    if (transform or unsupported_summary_modifier(
            retained_text, validated_stock_symbols=validated_stock_symbols)
            or legacy_message_scope_error
            or source_scope_error
            or source_range_error or adjacent_residue or source_expression_residue
            or constraint_error
            or unresolved_subset or unknown_section or ambiguous_section_scope
            or ((refers_back or named_report) and not plain_reference)
            or (refers_back and not sources and not artifact)):
        content_error = CONTENT_QUESTION
        artifact = ""
    if not sources and not artifact and not content_error:
        return None
    if artifact:
        sources = []
    args = {
        source: (scoped_args[source] if source in scoped_args
                 else _source_args(
                     source,
                     segmentation.source_text(source, retained_sources),
                     source_ranges.get(source, "")))
        for source in sources
    }
    if "calendar" in constraint_scopes and "calendar" in args:
        if set(args["calendar"]) - {"days", "period"}:
            content_error = CONTENT_QUESTION
        else:
            args["calendar"] = {"period": constraint_scopes["calendar"]}
    if "reminder" in constraint_scopes and "reminder" in args:
        reminder_args = args["reminder"]
        requested_scope = constraint_scopes["reminder"]
        if (not isinstance(reminder_args, dict)
                or reminder_args.get("scope") not in {"all", requested_scope}):
            content_error = CONTENT_QUESTION
        else:
            args["reminder"] = {**reminder_args, "scope": requested_scope}
    constraint_plan_error = False
    if constraint_ledger.selected_sources:
        constraint_plan_error = (
            set(sources) != set(constraint_ledger.selected_sources)
            or not set(sources).issubset(original_sources))
    for source, scope in constraint_scopes.items():
        if source not in args:
            constraint_plan_error = True
        elif (not isinstance(args[source], dict)
              or source == "reminder" and args[source].get("scope") != scope):
            constraint_plan_error = True
        elif source == "calendar" and args[source].get("period") != scope:
            constraint_plan_error = True
    if constraint_plan_error:
        content_error = CONTENT_QUESTION
    recipient = extract_recipient(text)
    channel = extract_channel(text)
    delivery_text = _mask_message_conversation_binding(text)
    delivery = ("draft" if _DRAFT.search(delivery_text)
                else ("scheduled" if _SCHEDULE.search(delivery_text) else "send"))
    # Immediate self-delivery uses the reviewable draft path, matching the
    # existing outbound safety rule. Explicit future sends still use the
    # scheduled queue and its confirmation.
    if recipient == "me" and delivery == "send":
        delivery = "draft"
    plan = WorkflowPlan(
        sources=sources,
        source_args=args,
        recipient=recipient,
        channel=channel,
        delivery=delivery,
        when=extract_delivery_time(text) if delivery == "scheduled" else "",
        location=(args.get("weather") or {}).get("location", ""),
        stock_symbols=(args.get("stock") or {}).get("symbols", []),
        date_range=date_range,
        original_request=original,
        artifact_text=artifact,
        artifact_request="",
        content_error=content_error,
    )
    plan.recompute_status()
    return plan


def compile_decision(plan: WorkflowPlan) -> RouteDecision:
    if plan.content_error:
        raise ValueError("Outbound content must be clarified before compiling an effect")
    source_tools = [SOURCE_TO_TOOL[source] for source in plan.sources]
    named_recipient = plan.recipient not in {"", "me"} and not (
        _EMAIL.fullmatch(plan.recipient) or _PHONE.fullmatch(plan.recipient))
    contact_tools = ["lookup_contact"] if named_recipient else []
    if plan.delivery == "scheduled":
        effect = "schedule_send"
    elif plan.delivery == "draft":
        effect = "draft_message" if plan.channel == "messages" else "draft_email"
    else:
        effect = "send_message" if plan.channel == "messages" else "send_email"

    direct_calls = [(SOURCE_TO_TOOL[source], plan.source_args.get(source, {}))
                    for source in plan.sources]
    if named_recipient:
        direct_calls.append(("lookup_contact", {"name": plan.recipient}))
    offered = list(dict.fromkeys(source_tools + contact_tools + [effect]))
    groups = tuple(frozenset({name}) for name in offered)
    all_effects = {"send_message", "send_email", "draft_message", "draft_email",
                   "schedule_send", "forward_email", "reply_to_email"}
    # Source arguments are workflow-owned too. A failed stock/news lookup may
    # be retried, but the model cannot silently narrow four requested symbols
    # to the one that happened to succeed or widen a date range on retry.
    bindings: dict[str, dict] = {
        SOURCE_TO_TOOL[source]: dict(plan.source_args.get(source, {}))
        for source in plan.sources
    }
    bindings[effect] = {"to": plan.recipient}
    if plan.artifact_text:
        # Copy exactly; the model must not rewrite, fabricate, or execute text
        # from a referenced answer while composing the delivery.
        payload_key = "text" if effect in {"send_message", "draft_message"} else "body"
        bindings[effect][payload_key] = plan.artifact_text
    if effect == "schedule_send":
        bindings[effect].update({
            "channel": "message" if plan.channel == "messages" else "email",
            "when": plan.when,
        })
    return RouteDecision(
        role="agent",
        model=role_to_model("agent"),
        needs_tools=True,
        source="workflow",
        reason=("typed grounded delivery workflow: "
                + " -> ".join(source_tools + contact_tools + [effect])),
        route_source="workflow",
        expect_tool_first=False,
        tool_subset=offered,
        light_read=False,
        multi_round=True,
        narration_after=frozenset(offered),
        direct_calls=direct_calls,
        required_tool_groups=groups,
        forbidden_tools=frozenset(all_effects - {effect}),
        tool_argument_bindings=bindings,
    )
