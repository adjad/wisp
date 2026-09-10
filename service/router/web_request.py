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
    recipient: str | None = None


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
    delivery_cancelled: bool = False
    presentations: tuple[Clause, ...] = ()

    @property
    def private(self) -> bool:
        return self.provenance in {Provenance.PRIVATE, Provenance.LOCAL}

    @property
    def time_scope(self) -> str | None:
        return self.scopes[-1].text if self.scopes else None

    @property
    def allowed(self) -> bool:
        return bool(self.query) and self.provenance == Provenance.EXTERNAL and not (self.opted_out or self.clarification)

    @property
    def authorized_effects(self) -> frozenset[str]:
        effects = {c.action for c in self.continuations if c.action and not c.negated}
        if self.delivery and not self.delivery_cancelled and self.delivery.channel:
            d = self.delivery
            effects.add("schedule_send" if d.scheduled else
                        ("draft_message" if d.channel == "messages" else "draft_email") if d.draft_only else
                        ("send_message" if d.channel == "messages" else "send_email"))
        return frozenset(effects)

    @property
    def authorized_tools(self) -> frozenset[str]:
        """Enabled effects plus their read prerequisites, not a domain menu."""
        names = {"web_search", *self.authorized_effects}
        if "append_note" in names:
            names.add("search_notes")
        if "add_reminder" in names:
            names.add("get_upcoming")
        if self.delivery and not self.delivery_cancelled and not (
                self.delivery.self_delivery or self.delivery.address or self.delivery.phone):
            names.add("lookup_contact")
        return frozenset(names)

    def source_request(self) -> WebRequest:
        return replace(self, delivery=None, continuations=(), write_intent=False)

    def continuation_request(self, clause: Clause) -> WebRequest:
        return replace(self, source=clause.text, clauses=(clause,), explicit=False,
                       current=False, provenance=clause.provenance, public_reference=False,
                       independent_task=True, inherited=False, scopes=clause.scopes,
                       delivery=None, query=None, continuations=(), write_intent=not clause.negated,
                       clarification=None)


@dataclass(frozen=True)
class Composition:
    source: str
    delivery: Delivery | None
    continuations: tuple[Clause, ...] = ()
    presentations: tuple[Clause, ...] = ()
    delivery_cancelled: bool = False


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
    (r"connect(?:s|ed|ing)?", "connect"),
)
_LOOKUP = "(?:" + "|".join(pattern for pattern, _ in _LOOKUP_LEXEMES) + ")"
_LOOKUP_CANONICAL = "(?:" + "|".join(lemma for _, lemma in _LOOKUP_LEXEMES) + ")"
_SOURCE_CUE = r"(?:web|websites?|internet|online|google|bing|external\s+sources?)"
_ACTION = (
    r"(?:send|forward|email|e-mail|text|message|draft|schedule|set|add|create|"
    r"delete|remove|move|open|close|launch|run|install|explain|write|debug|fix|remind|"
    r"translate|calculate|summarize|compare|implement|build|change|save|log|append|store|record|teach|define|check|keep|stay)"
)
_DELIVER = r"(?:send|forward|e-?mail|text|message|draft)"
_NEGATIVE = r"(?:don't|do not|not|never|stop|cancel|abort|skip|avoid|refrain from|without|with no|no)"
_PREFERENCE = r"i(?:'d|\s+would)?\s+prefer\s+not\s+to"
_DELIVERY_PREDICATE = r"(?:send(?:ing)?|forward(?:ing)?|e-?mail(?:ing)?|text(?:ing)?|messag(?:e|ing)|deliver(?:y|ing)?)"
_REVOKE = r"(?:deny|denied|disallow(?:ed)?|revoke[ds]?|withdraw(?:n)?|forbid(?:den)?|prohibit(?:ed)?|rescind(?:ed)?)"
_DENIED_STATE = r"(?:not\s+(?:allowed|authorized|permitted)|" + _REVOKE + ")"
_BOUNDARY = re.compile(
    r"[;!?\n]+|\.(?=\s|$)|,|"
    r"\s+(?=(?:without|don't|do not)\s+" + _DELIVERY_PREDICATE + r"\b)|"
    r"\s+(?:and(?:\s+(?:then|afterwards?))?|then|afterwards?|&|but)\s+(?=(?:" + _POLITE +
    r")(?:" + _ACTION + "|" + _NEGATIVE + "|" + _PREFERENCE + "|" + _REVOKE + r"|give|i\s+(?:do|revoke|withdraw|deny))\b)", re.I)
_AMOUNT = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a few|several|a couple of|an?)"
_UNIT = r"(?:minutes?|hours?|days?|weeks?|months?|quarters?|years?|hrs?|mins?)"
_TIME = re.compile(
    r"\b(?:(?:(?:in|over|during|for|within)\s+)?(?:(?:the|this)\s+)?(?:last|past|previous)\s+(?:" + _AMOUNT + r"\s+)?" + _UNIT + "|"
    + _AMOUNT + r"\s+" + _UNIT + r"\s+ago|"
    r"on\s+\d{4}-\d{2}-\d{2}|in\s+\d{4}|right\s+now|now|recently|"
    r"(?:since\s+)?(?:today|yesterday|tomorrow|tonight)|(?:since\s+)?(?:this|last|next)\s+"
    r"(?:morning|afternoon|evening|night|weekend|week|month|quarter|year)|over\s+the\s+weekend|"
    r"(?:on\s+)?(?:(?:last|next|this)\s+)?"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b(?:['’]s)?", re.I)
_FOLLOWUP = re.compile(r"^\s*(?:(?:and(?:\s+in)?|then|now|what about|how about|(?:the\s+)?same for)\s+(.+)|(.+?)\s+(?:too|as well|next|then|now)\s*[?.!]*\s*$)", re.I)
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


def _governing_consent(text: str) -> bool:
    """Instruction-level authorization operators; never search object text."""
    root = _lexical(text)
    network = _matches(r"\b(?:" + _SOURCE_CUE + r"|network|browse|search|access|connect)\b", root)
    if not network:
        return False
    # Active revocation, negative permission, and passive authorization state
    # share a network operand. Their subject/verb relation must be at the root.
    return (_matches(r"^(?:(?:i|we)\s+)?" + _REVOKE + r"\b", root)
            or _matches(r"^(?:i|we)\s+(?:do not|don't)\s+(?:consent|authorize|allow|permit|approve)\b", root)
            or _matches(r"^(?:[\w-]+\s+){0,3}(?:permission|authorization|consent|access)\s+(?:(?:is|was|has been)\s+)?" + _DENIED_STATE + r"\b", root)
            or _matches(r"^(?:stay|keep|remain)\s+(?:(?:me|us|it|this|that)\s+)?(?:off|offline|away\s+from)\b", root))


def _delivery_revocation(text: str) -> bool:
    root = _root(text)
    return (_matches(r"^(?:" + _NEGATIVE + "|" + _REVOKE + r")\s+(?:(?:the|any)\s+)?" + _DELIVERY_PREDICATE + r"\b", root)
            or _matches(r"^" + _DELIVERY_PREDICATE + r"\s+(?:(?:permission|authorization)\s+)?(?:is|was|has been)\s+" + _DENIED_STATE + r"\b", root)
            or (_matches(r"^(?:(?:i|we)\s+)?" + _REVOKE + r"\s+(?:permission|authorization|consent)\b", root)
                and _matches(r"\b" + _DELIVERY_PREDICATE + r"\b", root)))


def _presentation(text: str) -> bool:
    """An operation whose object corefers to the result, not another task."""
    root = _root(text)
    return (_matches(r"^(?:explain|summari[sz]e|teach(?:\s+me)?|give\s+me)\b", root)
            and (_matches(r"\b(?:it|this|that|them|findings|results|what\s+it\s+means)\b", root)
                 or _matches(r"^give\s+me\s+(?:an?\s+)?(?:brief|summary|explanation)\b", root)))


def _local_effect(text: str) -> str | None:
    """Type explicit local effect clauses only; source/topic text is excluded."""
    root = _root(text)
    if _matches(r"^(?:save|log|store|record)\b.*\b(?:(?:to|in|into)\s+(?:(?:my|apple)\s+)?notes|my\s+notes|an?\s+(?:new\s+)?note)\s*$", root):
        return "create_note"
    if _matches(r"^append\b.*\bnotes?\b", root):
        return "append_note"
    if _matches(r"^(?:(?:create|set|add)\s+(?:an?\s+)?reminder|remind\s+me)\b", root):
        return "add_reminder"
    if _matches(r"^(?:set|start)\s+(?:an?\s+)?timer\b", root):
        return "set_timer"
    if _matches(r"^(?:open|launch)\s+\S", root):
        return "open_app"
    return None


def _opt_out(clauses: tuple[Clause, ...]) -> bool:
    explicit_source = any(_explicit(clause.text) for clause in clauses)
    for clause in clauses:
        root = _lexical(clause.text)
        if _governing_consent(clause.text):
            return True
        # A lookup's noun-phrase object can name a work or phrase containing
        # negation. Its words are not imperatives to the assistant.
        if _explicit(clause.text) and _matches(
                r"\b(?:book|lyrics|documentary|film|song|campaign|slogans?|signs?)\s*$|\b(?:the\s+)?(?:phrase|title)\s+", root):
            continue
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
    recipient = address.group(0) if address else phone.group(0) if phone else None
    if recipient is None:
        destination = re.search(r"\bto\s+(.+?)(?=\s+(?:by|via|through|over|with|about|at)\b|$)", root, re.I)
        if destination:
            recipient = _recipient_atom(destination.group(1))
        else:
            recipient = _recipient_atom(re.sub(r"^" + _DELIVER + r"\s+", "", root, flags=re.I))
    return Delivery(text, channel, address.group(0) if address else None,
                    phone.group(0) if phone else None, self_delivery, draft_only, scheduled,
                    recipient=recipient)


def _recipient_atom(text: str) -> str | None:
    """A destination slot, not arbitrary text after a preposition."""
    atom = text.strip(" ,.!?")
    if _EMAIL.fullmatch(atom) or _PHONE.fullmatch(atom):
        return atom
    if _matches(r"^(?:the|an?|what|who|where|how|it|this|that|latest|current)\b", atom):
        return None
    if re.fullmatch(r"(?:me|myself|mom|dad|(?:my|our)\s+[\w'-]+(?:\s+[\w'-]+)?)", atom, re.I):
        return atom
    words = atom.split()
    if 0 < len(words) <= 4 and all(re.fullmatch(r"[A-Z][\w'-]*", word) for word in words):
        return atom
    return None


def _terminal_destination(payload: str) -> tuple[int, str] | None:
    """Recognize an outer destination, leaving ambiguous source attachments.

    A lone `to` under a source's on/about/regarding complement belongs to that
    complement (aid to Ukraine, tools to support refugees). A second terminal
    entity destination can close that complement, as in ...apply...to Mom.
    """
    relations = list(re.finditer(r"\s+to\s+", payload, re.I))
    if not relations:
        return None
    relation = relations[-1]
    recipient_text = re.split(r"\s+(?:at\s+\d|tomorrow\b|tonight\b|in\s+\d+\s+(?:minutes?|hours?))",
                              payload[relation.end():], maxsplit=1, flags=re.I)[0]
    recipient = _recipient_atom(recipient_text)
    if not recipient:
        return None
    if len(relations) == 1 and _matches(r"\b(?:on|about|regarding)\b", payload[:relation.start()]):
        return None
    return relation.start(), recipient


def split_delivery(text: str) -> tuple[str, Delivery | None]:
    composed = _compose(text, _clauses(text))
    return composed.source, composed.delivery


def _compose(text: str, clauses: tuple[Clause, ...]) -> Composition:
    """Separate a source from effect clauses, retaining original query bytes."""
    source_end, delivery, continuations = len(text), None, []
    presentations, cancelled = [], False

    def finish(source: str, effect: Delivery | None) -> Composition:
        return Composition(source, None if cancelled else effect, tuple(continuations),
                           tuple(presentations), cancelled)

    for index, clause in enumerate(clauses[1:], 1):
        root = _root(clause.text)
        separator = text[clauses[index - 1].end:clause.start]
        if _governing_consent(clause.text):
            source_end = min(source_end, clauses[index - 1].end)
            continue
        if _delivery_revocation(clause.text):
            if delivery or _delivery(clauses[0].text) or separator.strip():
                cancelled = True
                source_end = min(source_end, clauses[index - 1].end)
            continue
        action = re.match(r"(?:" + _NEGATIVE + r"\s+)?(?:" + _ACTION + r"|give)\b", root, re.I)
        if not action:
            continue
        topical = _matches(r"\bhow\s+to\b", text[:clause.start])
        if topical and separator.strip().lower() == "and" and not _presentation(clause.text) and not _matches(r"\b(?:it|them|summary|to)\b", root):
            continue
        source_end = min(source_end, clauses[index - 1].end)
        if _presentation(clause.text):
            presentations.append(clause)
            continue
        if re.fullmatch(r"keep\s+(?:it|this|that|the\s+answer)\s+offline", root, re.I):
            continue
        effect = _delivery(clause.text)
        if effect and delivery is None:
            delivery = replace(effect, start=clause.start)
        else:
            continuations.append(Clause(clause.text, clause.start, clause.end,
                                        bool(re.match(_NEGATIVE + r"\b", root, re.I)),
                                        _provenance(clause.text)[0], _scopes(clause.text),
                                        _local_effect(clause.text)))
    source = text[:source_end].rstrip(" ,;.!?") if source_end < len(text) else text
    leading = _delivery(source)
    if leading:
        # Explicit recipient-before-payload relation, including channel nouns:
        # 'send a message to Mom with <source>'. Neither side is guessed from
        # a payload keyword embedded in a recipient or source noun phrase.
        leading_slots = re.match(
            r"^\s*" + _POLITE + _DELIVER + r"\s+(?:(?:an?\s+)?(?:e-?mail|text|message)\s+to\s+)?"
            r"(?P<recipient>.+?)\s+(?:with|about|containing)\s+(?P<source>.+)$", _unquoted(source), re.I)
        if leading_slots and (recipient := _recipient_atom(leading_slots.group("recipient"))):
            payload = source[leading_slots.start("source"):leading_slots.end("source")]
            delivery_text = source[:leading_slots.start("source")].rstrip()
            return finish(payload, replace(_delivery(delivery_text), recipient=recipient))
        # Consume anaphoric delivery before payload scanning. Literal
        # addresses/phones are opaque recipient atoms, never source markers.
        if _matches(r"^" + _DELIVER + r"\s+(?:it|this|that|them|the\s+(?:results?|findings|summary|update))\s+to\s+", _root(source)):
            return finish("", leading)
        # "email me after you search ...": the after-clause is the source,
        # not a scheduled delivery time. Offsets are measured on normalized
        # text, whose apostrophe translation preserves length.
        normalized = _normalize(source)
        marker_text = _unquoted(source)
        for atom in (*_EMAIL.finditer(marker_text), *_PHONE.finditer(marker_text)):
            marker_text = marker_text[:atom.start()] + " " * (atom.end() - atom.start()) + marker_text[atom.end():]
        prefix = re.match(r"^\s*" + _POLITE + _DELIVER + r"\s+", normalized, re.I)
        if prefix:
            marker = re.search(
                r"\b(?:(?:the|an?|my)\s+)?(?:(?:latest|current|breaking|today's|yesterday's|tomorrow's)\b|"
                r"news\b|headlines?\b|report\b|summary\b|update\b|what\b|how\b)",
                marker_text[prefix.end():], re.I)
            after = re.search(r"\s+(?:after|once)\s+(?:you\s+)?(?=" + _LOOKUP + r"\b)", _unquoted(source), re.I)
            if after and (not marker or after.start() < prefix.end() + marker.start()):
                return finish(source[after.end():].strip(), _delivery(source[:after.start()]))
            if marker:
                begin = prefix.end() + marker.start()
                recipient_prefix = _recipient_atom(normalized[prefix.end():begin])
                if _matches(r"^the\s+(?:(?:latest|current|breaking)\s+)?(?:news|headlines?)\b", normalized[begin:]):
                    begin += 4
                # A recipient preceding the payload is already bound. Source
                # prepositions ('how to apply', 'after the summit') remain data.
                payload = _unquoted(source)[begin:]
                suffix = re.search(r"\s+(?:by|via|through|over)\s+(?:email|e-mail|text|sms|messages)\b", payload, re.I)
                recipient = recipient_prefix
                if not recipient_prefix:
                    destination = _terminal_destination(payload[:suffix.start() if suffix else len(payload)])
                    if destination:
                        offset, recipient = destination
                        end = begin + offset
                    else:
                        end = begin + suffix.start() if suffix else len(source)
                else:
                    end = begin + suffix.start() if suffix else len(source)
                delivery_text = (source[:begin] + source[end:]).strip()
                parsed_delivery = _delivery(delivery_text)
                if parsed_delivery:
                    parsed_delivery = replace(parsed_delivery, target_missing=recipient is None, recipient=recipient)
                return finish(source[begin:end].strip(), parsed_delivery)
        # Pure delivery continuations get their source from typed history.
        return finish("", leading)
    return finish(source, delivery)


def _provenance(source: str, *, fragment: bool = False) -> tuple[Provenance, bool]:
    # Quotes mask commands for consent/effect parsing, not source ownership.
    # Searching a quoted private-source phrase still needs the privacy guard.
    t = _normalize(source)
    # Explicit metalinguistic framing makes the quote a public title/topic,
    # not the referenced person's store. Only that framed span is masked;
    # independent ownership elsewhere in the source remains visible.
    t = re.sub(r'''(?<!\w)(["']).*?\1(?=\s+(?:slogans?|signs?|campaigns?|titles?)\b)''',
               lambda match: " " * len(match.group(0)), t, flags=re.I)
    if _matches(r"(?:^|\s)~?[/\\][\w.\-/\\]+|[\x60]{3}", t):
        return Provenance.LOCAL, False
    nouns = "(?:" + _PERSONAL + "|" + _ARTIFACT + ")"
    # Ownership is a source relation, not a finite list of resource nouns.
    # Explicit technical heads cannot launder a separate private/local source.
    if _matches(r"\b(?:my|our|your|their|his|her|private|internal|confidential|personal|unpublished|local)\s+\S", t):
        return Provenance.PRIVATE, False
    # Locative deixis points to the user's environment regardless of the noun
    # used for the device/store. Definite state/content locations are likewise
    # not public subjects merely because the requested fact is current.
    if _matches(r"\b(?:on|in|inside|from|within)\s+(?:this|that|these|those)\s+\S", _without_scope(t, _scopes(t))):
        return Provenance.LOCAL, False
    if (_matches(r"\b(?:on|inside)\s+the\s+(?!" + _SOURCE_CUE + r"\b)\S", t)
            and (_matches(r"^(?:what|which|show|tell)\b.*\b(?:running|changed|happened|happening|new)\b", _root(t))
                 or _matches(r"\bfor\s+the\s+\S", t))):
        return Provenance.LOCAL, False
    if _matches(r"\b(?:in|on|from|under)\s+(?:the\s+)?(?:Downloads|Desktop|Documents)\b", t):
        return Provenance.LOCAL, False
    if fragment and _matches(r"\b(?:downloads|desktop|documents)\s+folder\b", t):
        return Provenance.LOCAL, False
    if (_matches(r"\b" + _OWNED + r"\b[^,;.!?\n]*\b" + nouns + r"\b", t)
            or _matches(r"\b" + nouns + r"\s+(?:of|for|from|belonging\s+to)\s+" + _OWNED + r"\b", t)):
        return Provenance.PRIVATE, False
    message_source = re.search(r"\b(?:messages?|texts?|emails?|e-mails?)\s+(?:sent\s+)?(?:by|from|to)\s+\S", t, re.I)
    if message_source and not _matches(r"\bhow\s+to\b", t[:message_source.start()]):
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
                   or _matches(r"^(?:(?:latest|current|new)\s+)?(?:[\w-]+\s+){0,3}(?:source\s+code|API|SDK|CLI|releases?|library|crate|change[ -]?log)\b", head)
                   or _matches(r"^(?:(?:[\w-]+)\s+){0,2}(?:developer|technical|command[ -]line)\s+(?:docs?|documentation|spec(?:ification)?|tools?)\b", head))
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


def _parse_request(text: str, last_user: str | None = None, *,
                   recent_users: tuple[str, ...] = (), last_assistant: str | None = None) -> WebRequest:
    all_clauses = _clauses(text)
    composed = _compose(text, all_clauses)
    source, delivery, continuations = composed.source, composed.delivery, composed.continuations
    clauses = _clauses(source)
    scopes = _scopes(source)
    explicit = _explicit(source)
    opted_out = _opt_out(all_clauses)
    followup = _FOLLOWUP.match(_normalize(source))
    if followup and followup.group(2) and (_explicit(source) or _matches(
            r"^(?:what|who|where|when|which|how|give|tell|show|has|have|did|is|are|will)\b", _root(source))):
        followup = None  # A complete current question is not a topic fragment.
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
            candidate = _parse_request(item)
            if candidate.allowed or candidate.private:
                previous = candidate
                break
            if candidate.delivery or _matches(r"^\s*(?:yes|ok|okay|sure)\b", item):
                continue
            break  # An independent intervening task ends the source context.
    if acknowledgement and previous and previous.delivery and not pending_offer:
        acknowledgement_without_offer = True
    cancelled = composed.delivery_cancelled or bool(acknowledgement and previous and previous.delivery_cancelled)
    if cancelled and acknowledgement:
        acknowledgement_without_offer = True
    if previous and not cancelled and (delivery and not source or acknowledgement and pending_offer and previous.delivery):
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
    if query and any(not clause.negated and clause.action is None for clause in continuations):
        clarification = "Please clarify the separate action to perform after the public lookup."
    write_intent = delivery is not None or bool(continuations)
    return WebRequest(source, clauses, explicit, current, opted_out, provenance, public,
                      independent, inherited, scopes, delivery, query, continuations, write_intent, clarification,
                      acknowledgement_without_offer, cancelled, composed.presentations)


def classify(text: str, last_user: str | None = None, *,
             recent_users: tuple[str, ...] = (), last_assistant: str | None = None) -> WebRequest:
    """The sole entry point: freeze current instruction and user-turn context.

    History is parsed here, never downstream and never from tool-result text.
    """
    return _parse_request(text, last_user, recent_users=recent_users, last_assistant=last_assistant)
