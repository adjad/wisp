"""Conservative, clause-aware classification of public web requests.

This module is pure: it never reads personal stores, selects models, or executes
tools. Source provenance and consent are decided before query composition.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
    provenance: Provenance = Provenance.UNKNOWN
    scopes: tuple[TimeScope, ...] = ()
    action: str | None = None


@dataclass(frozen=True)
class TimeScope:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Delivery:
    text: str
    channel: str | None
    address: str | None = None
    phone: str | None = None
    self_delivery: bool = False
    draft_only: bool = False
    scheduled: bool = False
    start: int = 0
    target_missing: bool = False


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
    acknowledgement_without_offer: bool = False

    @property
    def private(self) -> bool:
        return self.provenance in {Provenance.PRIVATE, Provenance.LOCAL}

    @property
    def time_scope(self) -> str | None:
        return self.scopes[-1].text if self.scopes else None

    @property
    def allowed(self) -> bool:
        return bool(self.query) and self.provenance == Provenance.EXTERNAL and not (self.opted_out or self.clarification)

    def source_request(self) -> WebRequest:
        return replace(self, delivery=None, continuations=(), write_intent=False)

    def continuation_request(self, clause: Clause) -> WebRequest:
        return replace(self, source=clause.text, clauses=(clause,), explicit=False,
                       current=False, provenance=clause.provenance, public_reference=False,
                       independent_task=True, inherited=False, scopes=clause.scopes,
                       delivery=None, query=None, continuations=(), write_intent=not clause.negated,
                       clarification=None)


def _matches(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.I))


def _normalize(text: str) -> str:
    return text.translate(str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "＇": "'", "“": '"', "”": '"'}))


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


_POLITE = r"(?:(?:please|can you|could you|would you|will you|would you mind|i need you to|i want you to|i would like you to|i'd like you to|kindly|and|but|also|then|afterwards|afterward)\s+)*"
# One inflection vocabulary is shared by positive intent and its negation.
# A topic can mention these words without being an assistant command.
_LOOKUP_LEXEMES = (
    (r"search(?:es|ed|ing)?", "search"), (r"research(?:es|ed|ing)?", "research"),
    (r"brows(?:e|es|ed|ing)", "browse"),
    (r"look(?:s|ed|ing)?\s+(?:(?:it|this|that)\s+)?(?:up|online)|lookup", "lookup"),
    (r"fetch(?:es|ed|ing)?", "fetch"), (r"find(?:s|ing)?", "find"),
    (r"check(?:s|ed|ing)?", "check"), (r"consult(?:s|ed|ing)?", "consult"),
    (r"us(?:e|es|ed|ing)", "use"), (r"googl(?:e|es|ed|ing)", "google"),
    (r"bing(?:ed|ing)?", "bing"), (r"go(?:es|ing)?\s+online", "online"),
    (r"quer(?:y|ies|ied|ying)", "query"), (r"access(?:es|ed|ing)?", "access"),
    (r"visit(?:s|ed|ing)?", "visit"), (r"look(?:s|ed|ing)?\s+on", "lookon"),
)
_LOOKUP = "(?:" + "|".join(pattern for pattern, _ in _LOOKUP_LEXEMES) + ")"
_LOOKUP_CANONICAL = "(?:" + "|".join(lemma for _, lemma in _LOOKUP_LEXEMES) + ")"
_SOURCE_CUE = r"(?:web|websites?|internet|online|google|bing|external\s+sources?)"
_ACTION = (
    r"(?:send|forward|email|e-mail|text|message|draft|schedule|set|add|create|"
    r"delete|remove|move|open|close|launch|run|install|explain|write|debug|fix|remind|"
    r"translate|calculate|summarize|compare|implement|build|change|save|log|append|store|teach|define|check|keep)"
)
_DELIVER = r"(?:send|forward|e-?mail|text|message|draft)"
_NEGATIVE = r"(?:don't|do not|not|never|stop|cancel|abort|skip|avoid|refrain from|without|with no|no)"
_PREFERENCE = r"i(?:'d|\s+would)?\s+prefer\s+not\s+to"
_BOUNDARY = re.compile(
    r"[;!?\n]+|\.(?=\s|$)|,|"
    r"\s+(?:and(?:\s+(?:then|afterwards?))?|then|afterwards?|&|but)\s+(?=(?:" + _POLITE +
    r")(?:" + _ACTION + "|" + _NEGATIVE + "|" + _PREFERENCE + r")\b)", re.I)
_AMOUNT = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a few|several|a couple of|an?)"
_UNIT = r"(?:minutes?|hours?|days?|weeks?|months?|quarters?|years?|hrs?|mins?)"
_TIME = re.compile(
    r"\b(?:(?:(?:in|over|during|for|within)\s+)?(?:the\s+)?(?:last|past|previous)\s+" + _AMOUNT + r"\s+" + _UNIT + "|"
    + _AMOUNT + r"\s+" + _UNIT + r"\s+ago|"
    r"on\s+\d{4}-\d{2}-\d{2}|in\s+\d{4}|right\s+now|now|recently|"
    r"(?:since\s+)?(?:today|yesterday|tomorrow|tonight)|(?:since\s+)?(?:this|last|next)\s+"
    r"(?:morning|afternoon|evening|night|weekend|week|month|quarter|year)|over\s+the\s+weekend|"
    r"(?:on\s+)?(?:(?:last|next|this)\s+)?"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b(?:['’]s)?", re.I)
_FOLLOWUP = re.compile(r"^\s*(?:(?:and(?:\s+in)?|what about|how about|(?:the\s+)?same for)\s+(.+)|(.+?)\s+(?:too|as well)\s*[?.!]*\s*$)", re.I)
# Nouns describe the source, not the identity of a company or person.
_PERSONAL = (
    r"(?:messages?|texts?|e-?mails?|inbox|accounts?|health|medical|salary|payroll|"
    r"tax(?:\s+returns?)?|finances?|passwords?|credentials?|appointments?|"
    r"schedules?|calendars?|contacts?|browsing\s+history|passport|address|bank|ssn|social\s+security|jira|slack)"
)
_ARTIFACT = (
    r"(?:files?|folders?|downloads?|desktop|documents?|pdf|spreadsheets?|"
    r"presentations?|codebase|code|source\s+tree|sprint\s+backlog|backlog|"
    r"functions?|scripts?|repos?|repository|projects?|roadmaps?|notes?|"
    r"reminders?|timers?|apps?|screen|computer|mac|volume|wi-?fi|bluetooth|"
    r"battery|clipboard|pull\s+requests?|PRs?|issues?|tickets?)"
)
_OWNED = r"(?:my|our|your|their|his|her|team|shared|private|internal|confidential|personal)"
_OWNER = re.compile(r"\b(?P<owner>[\w-]+)(?:'s|s')\s+(?P<resource>[^,;.!?\n]+)", re.I)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")


def _root(text: str) -> str:
    return re.sub(r"^\s*" + _POLITE, "", _unquoted(text), flags=re.I).strip()


def _lexical(text: str) -> str:
    result = _root(text).casefold()
    for pattern, lemma in _LOOKUP_LEXEMES:
        result = re.sub(r"\b(?:" + pattern + r")\b", lemma, result)
    return result


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
    root = _lexical(text)
    command = _matches(r"^(?:" + _LOOKUP_CANONICAL + r"|web\s+search|do|perform|run|give|show|tell)\b", root)
    return command and bool(re.search(r"\b" + _SOURCE_CUE + r"\b", _root(text), re.I))


def _opt_out(clauses: tuple[Clause, ...]) -> bool:
    explicit_source = any(_explicit(clause.text) for clause in clauses)
    for clause in clauses:
        root = _lexical(clause.text)
        # Negation governs the assistant's action only at a command root.
        root = re.sub(r"^i\s+(?:don't|do not)\s+want\s+you\s+to\s+", "don't ", root, flags=re.I)
        root = re.sub(r"^i\s+(?:(?:would|'d)\s+)?(?:rather|prefer)\s+(?:that\s+)?you\s+not\s+",
                      "don't ", root, flags=re.I)
        root = re.sub(r"^(?:can|could|would)\s+you\s+not\s+", "don't ", root, flags=re.I)
        root = re.sub(r"^" + _PREFERENCE + r"\s+", "don't ", root, flags=re.I)
        concept = r"(?:(?:do|perform|run|make)\s+)?(?:(?:the|that|this|any|an?|more)\s+)*(?:" + _LOOKUP_CANONICAL + "|" + _SOURCE_CUE + r")\b"
        if re.match(r"keep\s+(?:it|this|that|the\s+answer)\s+offline\b", root, re.I):
            return True
        if re.match(_NEGATIVE + r"\s+" + concept, root, re.I):
            return True
        if re.match(r"(?:no\s+(?:web|internet|browsing|online)|offline|"
                    r"without\s+(?:any\s+|a\s+|the\s+)?(?:web|internet|browsing|external)|"
                    r"with\s+no\s+browsing|(?:answer|respond|stay|remain)\s+offline|"
                    r"(?:answer|respond|use|rely)\b.*\b(?:knowledge|memory|know)\b)", root, re.I):
            return True
        # Unpunctuated task-level tails remain constraints. Subordinate topics
        # in an explicit search are data, including 'why people don't browse'.
        tail = r"\b(?:" + _NEGATIVE + r"\s+" + concept + r"|offline\s+only\b)"
        for negative in re.finditer(tail, root, re.I):
            before, after = root[:negative.start()], root[negative.end():]
            subordinate = (_matches(r"\b(?:that|which|who|whose|why|how\s+to)\b", before)
                           or _matches(r"\bfor\b.+\bto\s+" + _LOOKUP_CANONICAL + r"\b", before))
            resource_modifier = (_matches(r"\b(?:access|features?|capabilit(?:y|ies)|functionality)\b", after)
                                 and _matches(r"\bfor\s+(?:\w+\s+){0,2}\w+s\s*$", before)
                                 and not _matches(r"\b(?:news|headlines?)\s*$", before))
            absent_resource = negative.group(0).startswith("with no") and not _matches(r"\b" + _LOOKUP_CANONICAL + r"\b", negative.group(0))
            # A negative nominal in the object of a lookup is a topic, not an
            # imperative. Task-level negatives still govern separate clauses.
            nominal_topic = (_explicit(clause.text) and _matches(r"\bfor\b", before)
                             and _matches(r"^no\b", negative.group(0)) and bool(after.strip()))
            if explicit_source and (subordinate or resource_modifier or absent_resource or nominal_topic):
                continue
            return True
    return False


def _delivery(text: str) -> Delivery | None:
    root = _root(text)
    if not re.match(_DELIVER + r"\b", root, re.I):
        return None
    # Object-only tutorial predicates have no recipient relation.
    if re.match(_DELIVER + r"\s+(?:messages?|texts?|emails?)\b", root, re.I) and not _matches(r"\bto\s+\S", root):
        return None
    address, phone = _EMAIL.search(root), _PHONE.search(root)
    channel = ("email" if address or _matches(r"^(?:e-?mail)\b|\b(?:by|via|through|over)\s+e-?mail\b|\bto\s+my\s+(?:email|inbox)\b|^(?:send|draft)\s+an?\s+email\b", root)
               else "messages" if phone or _matches(r"^(?:text|message)\b|\b(?:by|via)\s+(?:text|sms|messages)\b|^(?:send|draft)\s+a\s+(?:text|message)\b", root)
               else None)
    self_delivery = _matches(r"^(?:" + _DELIVER + r")\s+me\b|\b(?:to|via|through)\s+(?:me|myself|my\s+(?:email|inbox))\b", root)
    draft_only = self_delivery or _matches(r"\bdraft\b", root)
    scheduled = _matches(r"\b(?:tomorrow|tonight|later|at\s+\d+(?::\d+)?(?:am|pm)?|in\s+\d+\s+(?:minutes?|hours?))\b", root)
    return Delivery(text, channel, address.group(0) if address else None,
                    phone.group(0) if phone else None, self_delivery, draft_only, scheduled)


def split_delivery(text: str) -> tuple[str, Delivery | None]:
    source, delivery, _continuations = _compose(text, _clauses(text))
    return source, delivery


def _compose(text: str, clauses: tuple[Clause, ...]) -> tuple[str, Delivery | None, tuple[Clause, ...]]:
    """Separate a source from effect clauses, retaining original query bytes."""
    source_end, delivery, continuations = len(text), None, []
    for index, clause in enumerate(clauses[1:], 1):
        root = _root(clause.text)
        action = re.match(r"(?:" + _NEGATIVE + r"\s+)?" + _ACTION + r"\b", root, re.I)
        if not action:
            continue
        separator = text[clauses[index - 1].end:clause.start]
        topical = _matches(r"\bhow\s+to\b", text[:clause.start])
        if topical and separator.strip().lower() == "and" and not _matches(r"\b(?:it|them|summary|to)\b", root):
            continue
        source_end = min(source_end, clauses[index - 1].end)
        if re.fullmatch(r"summari[sz]e\s+(?:it|that|this|them|the\s+(?:results|findings))\s*|keep\s+(?:it|this|that|the\s+answer)\s+offline", root, re.I):
            continue  # Presentation of the source result, not an external effect.
        effect = _delivery(clause.text)
        if effect and delivery is None:
            delivery = replace(effect, start=clause.start)
        else:
            continuations.append(Clause(clause.text, clause.start, clause.end,
                                        bool(re.match(_NEGATIVE + r"\b", root, re.I)),
                                        _provenance(clause.text)[0], _scopes(clause.text),
                                        "create_note" if re.search(
                                            r"^(?:save|log|store)\b.*\b(?:to\s+(?:(?:my|apple)\s+)?notes|my\s+notes|an?\s+(?:new\s+)?note)\s*$",
                                            root, re.I) else None))
    source = text[:source_end].rstrip(" ,;.!?") if source_end < len(text) else text
    leading = _delivery(source)
    if leading:
        # "email me after you search ...": the after-clause is the source,
        # not a scheduled delivery time. Offsets are measured on normalized
        # text, whose apostrophe translation preserves length.
        normalized = _normalize(source)
        prefix = re.match(r"^\s*" + _POLITE + _DELIVER + r"\s+", normalized, re.I)
        if prefix:
            marker = re.search(
                r"\b(?:(?:the|an?|my)\s+)?(?:(?:latest|current|breaking|today's|yesterday's|tomorrow's)\b|"
                r"news\b|headlines?\b|report\b|summary\b|update\b|what\b|how\b)",
                _unquoted(source)[prefix.end():], re.I)
            after = re.search(r"\s+(?:after|once)\s+(?:you\s+)?(?=" + _LOOKUP + r"\b)", _unquoted(source), re.I)
            if after and (not marker or after.start() < prefix.end() + marker.start()):
                return source[after.end():].strip(), _delivery(source[:after.start()]), tuple(continuations)
            if marker:
                begin = prefix.end() + marker.start()
                recipient_prefix = normalized[prefix.end():begin].strip()
                if _matches(r"^the\s+(?:(?:latest|current|breaking)\s+)?(?:news|headlines?)\b", normalized[begin:]):
                    begin += 4
                # A recipient preceding the payload is already bound. Source
                # prepositions ('how to apply', 'after the summit') remain data.
                payload = _unquoted(source)[begin:]
                suffix = re.search(r"\s+(?:by|via|through|over)\s+(?:email|e-mail|text|sms|messages)\b", payload, re.I)
                recipient_found = bool(recipient_prefix)
                if not recipient_prefix:
                    # In recipient-last syntax the terminal 'to <recipient>'
                    # relation wins, never an earlier source-internal infinitive.
                    # Restrict the search to before the optional channel suffix.
                    recipients = list(re.finditer(r"\s+to\s+", payload[:suffix.start() if suffix else len(payload)], re.I))
                    if recipients and not _matches(
                            r"\b(?:how(?:\s+\w+){0,2}|ways?|steps?|methods?|in order)\s*$",
                            payload[:recipients[-1].start()]):
                        suffix = recipients[-1]
                        recipient_found = True
                end = begin + suffix.start() if suffix else len(source)
                delivery_text = (source[:begin] + source[end:]).strip()
                parsed_delivery = _delivery(delivery_text)
                if parsed_delivery:
                    parsed_delivery = replace(parsed_delivery, target_missing=not recipient_found)
                return source[begin:end].strip(), parsed_delivery, tuple(continuations)
        # Pure delivery continuations get their source from typed history.
        return "", leading, tuple(continuations)
    return source, delivery, tuple(continuations)


def _provenance(source: str, *, fragment: bool = False) -> tuple[Provenance, bool]:
    # Quotes mask commands for consent/effect parsing, not source ownership.
    # Searching a quoted private-source phrase still needs the privacy guard.
    t = _normalize(source)
    # Explicit metalinguistic framing makes the quote a public title/topic,
    # not the referenced person's store. Only that framed span is masked;
    # independent ownership elsewhere in the source remains visible.
    t = re.sub(r'"[^"\n]+"(?=\s+(?:slogans?|signs?|campaigns?|titles?)\b)',
               lambda match: " " * len(match.group(0)), t, flags=re.I)
    if _matches(r"(?:^|\s)~?[/\\][\w.\-/\\]+|[\x60]{3}", t):
        return Provenance.LOCAL, False
    nouns = "(?:" + _PERSONAL + "|" + _ARTIFACT + ")"
    # Ownership is a source relation, not a finite list of resource nouns.
    # Explicit technical heads cannot launder a separate private/local source.
    if _matches(r"\b(?:my|our|your|their|his|her|private|internal|confidential|personal|unpublished|local)\s+\S", t):
        return Provenance.PRIVATE, False
    if _matches(r"\b(?:in|on|from|under)\s+(?:the\s+)?(?:Downloads|Desktop|Documents)\b", t):
        return Provenance.LOCAL, False
    if fragment and _matches(r"\b(?:downloads|desktop|documents)\s+folder\b", t):
        return Provenance.LOCAL, False
    if (_matches(r"\b" + _OWNED + r"\b[^,;.!?\n]*\b" + nouns + r"\b", t)
            or _matches(r"\b" + nouns + r"\s+(?:of|for|from|belonging\s+to)\s+" + _OWNED + r"\b", t)):
        return Provenance.PRIVATE, False
    if (_matches(r"\b(?:messages?|texts?|emails?|e-mails?)\s+(?:sent\s+)?(?:by|from|to)\s+\S", t)
            and not _matches(r"\bhow\s+to\s+(?:send|write|compose)\b", t)):
        return Provenance.PRIVATE, False
    if _matches(r"\b(?:calendars?|inbox|passport|address|bank|ssn|jira|slack|pull\s+requests?)\s+(?:of|for|from|belonging\s+to)\s+\S", t):
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
        if owner.group("owner").lower() in {"today", "yesterday", "tomorrow", "tonight"}:
            continue  # Temporal possessives are not resource owners.
        surface = (_matches(r"^(?:code\s+interpreter|public\b|open[ -]source)", head)
                   or _matches(r"^(?:(?:latest|current|new)\s+)?(?:[\w-]+\s+){0,3}(?:source\s+code|API|SDK|releases?|library|crate)\b", head))
        public |= surface
        ambiguous_owner |= not surface
    if ambiguous_owner:
        return Provenance.UNKNOWN, False
    if _matches(r"\b(?:this|that|these|those)\s+(?:\w+\s+){0,3}" + nouns + r"\b", t):
        return Provenance.LOCAL, False
    # A bare product name is not a request to read its local/private contents.
    # Ownership and demonstratives above are the provenance boundaries.
    return Provenance.EXTERNAL, public


def _independent(text: str, *, fragment: bool = False) -> bool:
    root = re.sub(r"^\s*(?:an?\s+|the\s+)?", "", _root(text), flags=re.I)
    if _matches(r"^(?:(?:tell|show)\s+me\s+(?:how|why)|how\s+(?:to|do|can|should|would)|why)\b", root):
        return True
    if _matches(r"^(?:how\s+(?:do|can|should|would)\s+(?:i|we|you)\s+)?" + _ACTION + r"\b", root):
        return True
    if _matches(r"\b(?:analysis|explanation|story|plot|novel|chapter|scene|function|reminder|timer|fictional)\b", root):
        return True
    if _matches(r"\b(?:in|within|of)\s+(?:(?:the|this|that)\s+)?(?:movie|film|book|episode|fiction)\b(?!\s+(?:industry|business|market|sector|bans?))", root):
        return True
    if _matches(r"^(?:give\b.*\bheads\s+up\b|tell\s+(?!me\b)\S+)", root):
        return True
    return fragment and _matches(r"^(?:writing|debugging|creating|setting|launching|changing|comparing|saving|logging|appending|opening|explaining|teaching|implementing|building|deleting)\b", root)


def _current(text: str, scopes: tuple[TimeScope, ...]) -> bool:
    t = _root(text)
    if _independent(t) and not _matches(r"^(?:give|show|tell|update|brief)\b", t):
        return False
    if _matches(r"\b(?:in|during)\s+(?:the\s+)?\d{4}s?\b", t) and not _matches(r"\b(?:latest|current|today|now)\b", t):
        return False
    cue = bool(scopes) or _matches(r"\b(?:latest|currently|current|new|recent|update|brief|briefing|developments?|breaking)\b", t)
    question_root = _without_scope(t, _scopes(t)).lstrip(" ,")
    question = _matches(r"^(?:what|who|where|when|which|name|will|how|has|have|did|does|is|are|any|give|show|tell|update|brief)\b", question_root)
    if _matches(r"\b(?:news|headlines?)\b", t):
        return True
    # Extract the grammatical remainder, not a roster of events or subjects.
    # Empty personal-agenda questions ("what's on today") have no external
    # subject; unfamiliar external facts ("did Zorvia ratify it today") do.
    remainder = _without_scope(t, _scopes(t))
    remainder = re.sub(r"\b(?:what's|who's|what|who|where|when|how|did|does|do|has|have|is|are|was|were|any|give|show|tell|me|an?|the|latest|current|currently|new|recent|update|updates|brief|briefing|developments?|developing|breaking|happening|happened|going|on|in|with|regarding|situation|status|changed|things)\b", " ", remainder, flags=re.I)
    remainder = re.sub(r"\b(?:of|for|about|to|from|it|that|this|you|we|i|doing|due|scheduled|queued)\b", " ", remainder, flags=re.I)
    external_subject = bool(re.search(r"[a-z0-9]", remainder, re.I))
    if _matches(r"\b(?:am\s+i|are\s+(?:we|you)|(?:do|did|have)\s+(?:i|we|you))\b", t):
        external_subject = False
    if re.fullmatch(r"\s*(?:volume|battery(?:\s+level)?|clipboard|wi-?fi|bluetooth|screen)\s*[?.!]*\s*", remainder, re.I):
        external_subject = False
    implicit_current = _matches(r"\b(?:happening|going\s+on)\s+(?:in|with)\b", t)
    noun_update = _matches(r"^(?:(?:the|an?)\s+)?(?:latest|current|recent|new|breaking|update|brief|briefing|summary|report)\b", t)
    return external_subject and ((cue and (question or noun_update)) or implicit_current)


def _scopes(text: str) -> tuple[TimeScope, ...]:
    return tuple(TimeScope(re.sub(r"'s$", "", _normalize(text[m.start():m.end()])), m.start(), m.end())
                 for m in _TIME.finditer(_unquoted(text)))


def _without_scope(text: str, scopes: tuple[TimeScope, ...]) -> str:
    result, start = [], 0
    for scope in scopes:
        result.append(text[start:scope.start])
        start = scope.end
    result.append(text[start:])
    return re.sub(r"\s+", " ", "".join(result)).strip(" ,?.!")


def _pending_offer(text: str | None, action: str) -> bool:
    if not text:
        return False
    masked = _unquoted(text)
    return (_matches(
        r"\b(?:(?:would you like|do you want)\s+(?:me\s+)?to|want me to|shall i|should i|may i|can i)\s+"
        + action + r"\b", masked)
        or _matches(r"\bi can\s+" + action + r"\b[^.!?]*\bif\s+you(?:'d|\s+would)?\s+(?:like|want)\b", masked))


def classify(text: str, last_user: str | None = None, *,
             recent_users: tuple[str, ...] = (), last_assistant: str | None = None) -> WebRequest:
    all_clauses = _clauses(text)
    source, delivery, continuations = _compose(text, all_clauses)
    clauses = _clauses(source)
    scopes = _scopes(source)
    explicit = _explicit(source)
    opted_out = _opt_out(all_clauses)
    followup = _FOLLOWUP.match(_normalize(source))
    topic = next(group for group in followup.groups() if group is not None) if followup else source
    fragment = bool(followup) or bool(last_user and scopes and not _without_scope(source, scopes))
    provenance, public = _provenance(source, fragment=fragment)
    independent = not explicit and _independent(
        _without_scope(topic, _scopes(topic)), fragment=fragment)
    current = not independent and not followup and _current(source, scopes)
    query = source if (explicit or current) and provenance == Provenance.EXTERNAL else None
    inherited, clarification = False, None
    acknowledgement = _matches(r"^\s*(?:yes|yeah|yep|sure|ok|okay|please do|go ahead|do it)[.!\s]*$", text)
    # An acknowledgement authorizes only a pending outbound proposition, not
    # any assistant offer after an old send. Past-tense completion and offers
    # to explain/reformat the result do not match this grammatical relation.
    pending_offer = _pending_offer(last_assistant, _DELIVER)
    acknowledgement_without_offer = acknowledgement and not _pending_offer(last_assistant, _ACTION)
    previous = None
    if (fragment or (delivery and not source) or acknowledgement) and not (opted_out or independent):
        history = recent_users + ((last_user,) if last_user and (not recent_users or recent_users[-1] != last_user) else ())
        for item in reversed(history):
            candidate = classify(item)
            if candidate.allowed or candidate.private:
                previous = candidate
                break
            if candidate.delivery or _matches(r"^\s*(?:yes|ok|okay|sure)\b", item):
                continue
            break  # An independent intervening task ends the source context.
    if acknowledgement and previous and previous.delivery and not pending_offer:
        acknowledgement_without_offer = True
    if previous and (delivery and not source or acknowledgement and pending_offer and previous.delivery):
        if delivery and re.fullmatch(r"\s*(?:email|e-mail|text|messages)\s*[.!]?", text, re.I) and previous.delivery:
            delivery = replace(previous.delivery, channel=delivery.channel)
        delivery = delivery or previous.delivery
        source, clauses, scopes = previous.source, previous.clauses, previous.scopes
        provenance, public = previous.provenance, previous.public_reference
        query, inherited = previous.query, True
    elif fragment and query is None and previous:
        if previous.private:
            provenance, inherited = previous.provenance, True
        elif previous.allowed and provenance in {Provenance.EXTERNAL, Provenance.UNKNOWN}:
            topic = topic.strip(" ?.!")
            topic_scopes = _scopes(topic)
            bare_topic = _without_scope(topic, topic_scopes)
            if topic_scopes and re.fullmatch(r"(?:(?:the|latest)\s+)?(?:news|headlines?)", bare_topic, re.I):
                bare_topic = ""
            safe_topic = (provenance == Provenance.EXTERNAL
                          and bool(re.fullmatch(r"[\w][\w\s'.+-]*", bare_topic))
                          and len(bare_topic.split()) <= 20
                          and not _matches(r"\b(?:it|that|this|them|those|these|something|anything)\b", bare_topic))
            if public or not bare_topic or safe_topic:
                inherited = True
                if not bare_topic and topic_scopes:
                    previous_topic = _without_scope(previous.source, previous.scopes)
                    previous_topic = re.sub(r"\b(?:latest|current|recent(?:ly)?)\b", "", previous_topic, flags=re.I)
                    query = re.sub(r"\s+", " ", f"{previous_topic} {topic_scopes[-1].text}").strip()
                else:
                    news = "" if _matches(r"\b(?:news|headlines?)\b", topic) else " news"
                    query = f"{topic}{news}" + ("" if topic_scopes else f" {previous.time_scope or 'latest'}")
            else:
                clarification = "Which public topic should I look up?"
    if provenance == Provenance.UNKNOWN:
        if (explicit or current or fragment or last_user) and not opted_out:
            clarification = "Which public topic should I look up? Is this source public or private/local?"
        else:
            # Ordinary local questions ('what's Mom's number') retain their
            # local contracts. Unknown ownership still cannot authorize web.
            provenance = Provenance.PRIVATE
    if delivery and delivery.target_missing and query and not opted_out:
        clarification = "Who should receive the public findings?"
    write_intent = delivery is not None or bool(continuations)
    return WebRequest(source, clauses, explicit, current, opted_out, provenance, public,
                      independent, inherited, scopes, delivery, query, continuations, write_intent, clarification,
                      acknowledgement_without_offer)
