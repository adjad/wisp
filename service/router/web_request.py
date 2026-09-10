"""Conservative, clause-aware classification of public web requests.

This module is pure: it never reads personal stores, selects models, or executes
tools. Source provenance and consent are decided before query composition.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class Provenance(str, Enum):
    EXTERNAL = "external"
    PRIVATE = "private"
    LOCAL = "local"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Clause:
    text: str
    start: int
    end: int
    negated: bool = False


@dataclass(frozen=True)
class TimeScope:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Delivery:
    text: str
    channel: str | None


@dataclass(frozen=True)
class WebRequest:
    source: str
    clauses: tuple[Clause, ...]
    explicit: bool
    current: bool
    opted_out: bool
    provenance: Provenance
    public_reference: bool
    independent_task: bool
    inherited: bool
    scopes: tuple[TimeScope, ...]
    delivery: Delivery | None
    query: str | None
    continuations: tuple[Clause, ...] = ()
    write_intent: bool = False
    clarification: str | None = None

    @property
    def private(self) -> bool:
        return self.provenance in {Provenance.PRIVATE, Provenance.LOCAL}

    @property
    def time_scope(self) -> str | None:
        return self.scopes[-1].text if self.scopes else None

    @property
    def allowed(self) -> bool:
        return bool(self.query) and not (self.opted_out or self.private or self.clarification)


def _matches(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.I))


def _normalize(text: str) -> str:
    return text.translate(str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "＇": "'"}))


def _unquoted(text: str) -> str:
    """Mask quoted topics without changing offsets into the original request."""
    original = _normalize(text)
    chars = list(original)
    quote = None
    for i, char in enumerate(chars):
        apostrophe = (char == "'" and i > 0 and original[i - 1].isalnum()
                      and (quote is None or (i + 1 < len(original) and original[i + 1].isalnum())))
        if char in {"'", '"'} and not apostrophe:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            chars[i] = " "
        elif quote:
            chars[i] = " "
    return "".join(chars)


_POLITE = r"(?:(?:please|can you|could you|would you|i need you to|i want you to)\s+)?"
_LOOKUP = (
    r"(?:search|research|browse|look(?:\s+(?:it|this|that))?\s+up|look\s+online|"
    r"find|check|consult|use|go\s+online|do\s+(?:an?\s+)?(?:web|online)\s+search)"
)
_ACTION = (
    r"(?:send|forward|email|e-mail|text|message|draft|schedule|set|add|create|"
    r"delete|remove|move|open|close|launch|run|install|explain|write|debug|fix|remind|"
    r"translate|calculate|summarize|compare|implement|build|change)"
)
_DELIVER = r"(?:send|forward|e-?mail|text|message|draft)"
_NEGATIVE = r"(?:don't|do not|not|never|stop|cancel|abort|skip|avoid|refrain from)"
_BOUNDARY = re.compile(
    r"[;!?\n]+|\.(?=\s|$)|,|"
    r"\s+(?:and(?:\s+then)?|then|&|but)\s+(?=(?:" + _POLITE +
    r")(?:" + _ACTION + "|" + _NEGATIVE + r")\b)", re.I)
_TIME = re.compile(
    r"\b(?:in\s+the\s+(?:last|past)\s+\d+\s+(?:hours?|days?|weeks?)|"
    r"on\s+\d{4}-\d{2}-\d{2}|in\s+\d{4}|right\s+now|now|recently|"
    r"today|yesterday|tomorrow|tonight|(?:this|last|next)\s+"
    r"(?:morning|afternoon|evening|night|weekend|week|month|year)|"
    r"(?:on\s+)?(?:(?:last|next|this)\s+)?"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b(?:['’]s)?", re.I)
_FOLLOWUP = re.compile(r"^\s*(?:and(?:\s+in)?|what about|how about)\s+(.+)", re.I)
# Nouns describe the source, not the identity of a company or person.
_PERSONAL = (
    r"(?:messages?|texts?|e-?mails?|inbox|accounts?|health|medical|salary|payroll|"
    r"tax(?:\s+returns?)?|finances?|passwords?|credentials?|appointments?|"
    r"schedules?|calendars?|contacts?|browsing\s+history)"
)
_ARTIFACT = (
    r"(?:files?|folders?|downloads?|desktop|documents?|pdf|spreadsheets?|"
    r"presentations?|codebase|code|source\s+tree|sprint\s+backlog|backlog|"
    r"functions?|scripts?|repos?|repository|projects?|roadmaps?|notes?|"
    r"reminders?|timers?|apps?|screen|computer|mac|volume|wi-?fi|bluetooth|"
    r"battery|clipboard)"
)
_OWNED = r"(?:my|our|your|their|his|her|team|shared|private|internal|confidential|personal)"
_OWNER = re.compile(r"\b(?P<owner>[\w-]+)(?:'s|s')\s+(?P<resource>[^,;.!?\n]+)", re.I)


def _clauses(text: str) -> tuple[Clause, ...]:
    masked = _unquoted(text)
    result = []
    start = 0
    for match in _BOUNDARY.finditer(masked):
        if text[start:match.start()].strip():
            result.append(Clause(text[start:match.start()].strip(), start, match.start()))
        start = match.end()
    if text[start:].strip():
        result.append(Clause(text[start:].strip(), start, len(text)))
    return tuple(result)


def _explicit(text: str) -> bool:
    root = re.sub(r"^\s*" + _POLITE, "", _unquoted(text), flags=re.I)
    medium = r"(?:the\s+)?(?:web|internet|online|google|bing)"
    return bool(re.match(
        r"(?:(?:search|research|browse|check)\s+" + medium + r"\b|"
        r"(?:do|perform|run)\s+(?:an?\s+)?(?:web|online)\s+search\b|"
        r"(?:use\s+" + medium + r"\s+to|go\s+online\s+and)\s+" + _LOOKUP + r"\b|"
        r"(?:google|bing)\s+for\b|"
        r"look\s+online\b|"
        + _LOOKUP + r"\b[^;!?\n]*\b(?:online|on\s+" + medium + r"|using\s+" + medium + r")\b)",
        root, re.I))


def _opt_out(clauses: tuple[Clause, ...]) -> bool:
    for clause in clauses:
        root = re.sub(r"^\s*" + _POLITE, "", _unquoted(clause.text), flags=re.I)
        # Negation governs the assistant's action only at a command root.
        root = re.sub(r"^i\s+(?:don't|do not)\s+want\s+you\s+to\s+", "don't ", root, flags=re.I)
        root = re.sub(r"^i\s+(?:(?:would|'d)\s+)?(?:rather|prefer)\s+(?:that\s+)?you\s+not\s+",
                      "don't ", root, flags=re.I)
        root = re.sub(r"^(?:can|could|would)\s+you\s+not\s+", "don't ", root, flags=re.I)
        if re.match(_NEGATIVE + r"\s+(?:(?:the|that|this|any|a)\s+)?"
                    r"(?:" + _LOOKUP + r"|searching|browsing|researching|looking\s+(?:online|up)|web\s+search|online\s+lookup|"
                    r"using\s+(?:the\s+)?(?:web|internet)|going\s+online)\b", root, re.I):
            return True
        if re.match(r"(?:no\s+(?:web|internet|browsing|online)|offline|"
                    r"without\s+(?:any\s+|a\s+|the\s+)?(?:web|internet|browsing|external)|"
                    r"with\s+no\s+browsing|(?:answer|respond|stay|remain)\s+offline|"
                    r"(?:answer|respond|use|rely)\b.*\b(?:knowledge|memory|know)\b)", root, re.I):
            return True
        # Unpunctuated task-level tails remain constraints. Subordinate topics
        # in an explicit search are data, including 'why people don't browse'.
        tail = r"\b(?:without\s+(?:(?:a|any|the)\s+)?web\s+search|with\s+no\s+browsing|offline\s+only)\b"
        if not _explicit(root) and _matches(tail, root):
            return True
        if not _explicit(root) and _matches(
                r"\bwithout\s+(?:using\s+)?(?:the\s+)?(?:web|internet|external\s+sources)\b", root):
            return True
    return False


def _delivery(text: str) -> Delivery | None:
    root = re.sub(r"^\s*" + _POLITE, "", _unquoted(text), flags=re.I)
    if not re.match(_DELIVER + r"\b", root, re.I):
        return None
    # Object-only tutorial predicates have no recipient relation.
    if re.match(_DELIVER + r"\s+(?:messages?|texts?|emails?)\b", root, re.I) and not _matches(r"\bto\s+\S", root):
        return None
    channel = ("email" if _matches(r"^(?:e-?mail)\b|\b(?:by|via|through|over)\s+e-?mail\b", root)
               else "messages" if _matches(r"^(?:text|message)\b|\b(?:by|via)\s+(?:text|sms|messages)\b", root)
               else None)
    return Delivery(text, channel)


def split_delivery(text: str) -> tuple[str, Delivery | None]:
    clauses = _clauses(text)
    for index, clause in enumerate(clauses[1:], 1):
        if delivery := _delivery(clause.text):
            # A how-to topic with coordinated transport verbs stays intact.
            prefix = text[:clause.start]
            if _matches(r"\bhow\s+to\b", prefix) and not _matches(
                    r"\b(?:it|them|summary|to)\b", clause.text):
                continue
            end = clauses[index - 1].end
            return text[:end].rstrip(" ,;.!?"), delivery
    # Preserve the established one-clause report -> destination workflow.
    if re.match(r"^\s*" + _POLITE + _DELIVER + r"\b", _unquoted(text), re.I):
        prefix = re.match(r"^\s*" + _POLITE + _DELIVER + r"\s+", text, re.I)
        if prefix:
            report = re.search(r"\b(?:(?:latest|current|breaking|today's)\s+)?(?:news|headlines?|report|summary)\b",
                               text[prefix.end():], re.I)
            to = re.search(r"\s+to\s+", text[prefix.end():], re.I)
            if report:
                begin = prefix.end() + report.start()
                end = prefix.end() + to.start() if to and to.start() > report.start() else len(text)
                source = text[begin:end].strip()
                return source, _delivery(text)
    return text, None


def _provenance(source: str, *, fragment: bool = False) -> tuple[Provenance, bool]:
    t = _normalize(source)
    if _matches(r"(?:^|\s)~?[/\\][\w.\-/\\]+|[\x60]{3}", t):
        return Provenance.LOCAL, False
    nouns = "(?:" + _PERSONAL + "|" + _ARTIFACT + ")"
    if _matches(r"\b(?:my|our|your|his|her)\s+(?:week|day)\b", t):
        return Provenance.PRIVATE, False
    if (_matches(r"\b" + _OWNED + r"\b[^,;.!?\n]*\b" + nouns + r"\b", t)
            or _matches(r"\b" + nouns + r"\s+(?:of|for|from|belonging\s+to)\s+" + _OWNED + r"\b", t)):
        return Provenance.PRIVATE, False
    if _matches(r"\b(?:messages?|texts?|emails?|e-mails?)\s+(?:sent\s+)?(?:by|from|to)\s+\S", t):
        return Provenance.PRIVATE, False
    public, ambiguous_owner = False, False
    for position in re.finditer(r"\b", t):
        owner = _OWNER.match(t, position.start())
        if owner is None:
            continue
        if owner.group("owner").lower() in {"what", "who", "how", "it", "there", "here", "that", "let"}:
            continue
        resource = owner.group("resource")
        # Public resource grammar, not a company/name allowlist.
        # A later publisher/resource cannot launder the head's provenance:
        # "Mom's messages about the Python release" is still private mail.
        head = re.split(r"\s+(?:about|regarding|and|with|from|for|to)\b", resource, maxsplit=1, flags=re.I)[0]
        surface = (_matches(r"^(?:source\s+code|code\s+interpreter|public\b|open[ -]source)", head)
                   or _matches(r"^(?:(?:latest|current|new)\s+)?(?:[\w-]+\s+){0,2}(?:API|SDK|documentation|releases?)\b", head))
        kinship = _matches(r"^(?:mom|dad|mother|father|sister|brother|wife|husband|partner)$", owner.group("owner"))
        if kinship and _matches(r"\b" + nouns + r"\b", resource):
            return Provenance.PRIVATE, False
        technical_calendar = _matches(r"^calendar\s+(?:API|SDK)\b", head)
        if _matches(r"\b" + _PERSONAL + r"\b", head) and not technical_calendar:
            return Provenance.PRIVATE, False
        if _matches(r"\b(?:code|codebase|files?|folders?|notes?)\b", head) and not surface:
            return Provenance.PRIVATE, False
        public |= surface
        ambiguous_owner |= not surface and _matches(r"\b(?:projects?|roadmaps?)\b", head)
    if fragment and ambiguous_owner and not public:
        return Provenance.UNKNOWN, False
    if _matches(r"\b(?:this|that|these|those)\s+(?:\w+\s+){0,3}" + nouns + r"\b", t):
        return Provenance.LOCAL, False
    if fragment and _matches(r"\b(?:" + _PERSONAL + "|" + _ARTIFACT + r")\b", t) and not public:
        return Provenance.LOCAL, False
    return Provenance.EXTERNAL, public


def _independent(text: str, *, fragment: bool = False) -> bool:
    root = re.sub(r"^\s*(?:an?\s+|the\s+)?", "", _unquoted(text))
    if _matches(r"^(?:how\s+(?:do|can|should|would)\s+(?:i|we|you)\s+)?" + _ACTION + r"\b", root):
        return True
    if _matches(r"\b(?:explanation|story|plot|novel|chapter|scene|function|reminder|timer)\b", root):
        return True
    return fragment and _matches(r"\b[a-z]+ing\b", root)


def _current(text: str, scopes: tuple[TimeScope, ...]) -> bool:
    t = _unquoted(text)
    if _independent(t) and not _matches(r"^(?:give|show|tell|update|brief)\b", t):
        return False
    if _matches(r"\b(?:in|during)\s+(?:the\s+)?\d{4}s?\b", t) and not _matches(r"\b(?:latest|current|today|now)\b", t):
        return False
    cue = bool(scopes) or _matches(r"\b(?:latest|current|new|recent|update|brief|briefing|developments?|breaking)\b", t)
    question = _matches(r"^(?:what|who|where|when|how|has|have|did|does|is|are|any|give|show|tell|update|brief)\b", t)
    event = _matches(r"\b(?:news|headlines?|happening|happened|going\s+on|situation|"
                     r"developments?|developing|status|changed|election|won|winner|wildfires?|ceasefire|"
                     r"leader|president|prime\s+minister|release|version|update|updates)\b", t)
    if _matches(r"\b(?:news|headlines?)\b", t):
        return True
    subject = _matches(r"\b(?:in|on|with|regarding)\s+\S", _without_scope(t, scopes))
    if cue and question and subject:
        return True
    specific = _matches(r"\b(?:election|won|winner|wildfires?|ceasefire|leader|president|prime\s+minister|release|version)\b", t)
    return event and (((subject or specific) and cue and question) or _matches(r"\b(?:latest|current|recent|new)\b", t)
                      or _matches(r"\b(?:happening|going\s+on)\s+(?:in|with)\b", t))


def _scopes(text: str) -> tuple[TimeScope, ...]:
    return tuple(TimeScope(re.sub(r"['’]s$", "", text[m.start():m.end()]), m.start(), m.end())
                 for m in _TIME.finditer(_unquoted(text)))


def _without_scope(text: str, scopes: tuple[TimeScope, ...]) -> str:
    result, start = [], 0
    for scope in scopes:
        result.append(text[start:scope.start])
        start = scope.end
    result.append(text[start:])
    return re.sub(r"\s+", " ", "".join(result)).strip(" ,?.!")


def classify(text: str, last_user: str | None = None) -> WebRequest:
    source, delivery = split_delivery(text)
    clauses = _clauses(source)
    continuations = ()
    for index, clause in enumerate(clauses[1:], 1):
        root = re.sub(r"^\s*" + _POLITE, "", _unquoted(clause.text), flags=re.I)
        if re.match(r"(?:" + _NEGATIVE + r"\s+)?" + _ACTION + r"\b", root, re.I):
            separator = source[clauses[index - 1].end:clause.start]
            if _matches(r"\bhow\s+to\b", source[:clause.start]) and separator.strip().lower() == "and":
                continue
            continuations = tuple(Clause(c.text, c.start, c.end,
                                         bool(re.match(_POLITE + _NEGATIVE + r"\b", c.text, re.I)))
                                  for c in clauses[index:])
            source = source[:clauses[index - 1].end].rstrip(" ,;.!?")
            clauses = clauses[:index]
            break
    scopes = _scopes(source)
    explicit = _explicit(source)
    opted_out = _opt_out(_clauses(text))
    followup = _FOLLOWUP.match(_normalize(source))
    topic = followup.group(1) if followup else source
    provenance, public = _provenance(source, fragment=bool(followup))
    independent = not explicit and _independent(
        _without_scope(topic, _scopes(topic)), fragment=bool(followup))
    current = not independent and not followup and _current(source, scopes)
    query = source if (explicit or current) and provenance == Provenance.EXTERNAL else None
    inherited, clarification = False, None
    if followup and query is None and not (opted_out or independent) and last_user:
        previous = classify(last_user)
        if previous.private:
            provenance, inherited = previous.provenance, True
        elif previous.allowed and provenance in {Provenance.EXTERNAL, Provenance.UNKNOWN}:
            topic = topic.strip(" ?.!")
            topic_scopes = _scopes(topic)
            bare_topic = _without_scope(topic, topic_scopes)
            # Only named public topics or an unambiguous time-only fragment.
            public_category = _matches(
                r"^(?:the\s+)?(?:[\w-]+\s+){0,2}(?:industry|markets?|sector|economy|politics|science|technology|energy|biotech|education|sports)$",
                bare_topic)
            named_topic = re.fullmatch(r"(?:[A-Z][\w-]*\s*)+", bare_topic)
            if public or public_category or not bare_topic or named_topic:
                inherited = True
                if not bare_topic and topic_scopes:
                    previous_topic = _without_scope(previous.source, previous.scopes)
                    previous_topic = re.sub(r"\b(?:latest|current|recent(?:ly)?)\b", "", previous_topic, flags=re.I)
                    query = re.sub(r"\s+", " ", f"{previous_topic} {topic_scopes[-1].text}").strip()
                else:
                    query = f"{topic} news" + ("" if topic_scopes else f" {previous.time_scope or 'latest'}")
            else:
                clarification = "Which public topic should I look up?"
    write_intent = delivery is not None or bool(continuations)
    return WebRequest(source, clauses, explicit, current, opted_out, provenance, public,
                      independent, inherited, scopes, delivery, query, continuations, write_intent, clarification)
