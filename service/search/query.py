"""Query understanding — all deterministic, no model on the hot path.

The design doc's worked example fails not just on vocabulary but on SHAPE:
"What were the traits of Joe's dog and what color dog was he" is two questions,
and one embedding of the whole string is a blurry average of both. Splitting it
is the single highest-leverage step, and it's regex work, not inference.

Negation and filters are parsed here rather than left to the model on purpose:
an LLM interpreting "not" is a reliability regression, not a feature.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Leading interrogatives, or a trailing '?', mean the user wants an ANSWER
# rather than a list of places the word appears.
_QUESTION_RE = re.compile(
    r"^\s*(what|which|who|whom|whose|when|where|why|how|is|are|was|were|do|does|"
    r"did|can|could|should|would|will|has|have|had|list|show|summar|explain|tell)\b",
    re.I,
)

# Split points for multi-part questions. Only conjunctions that introduce a
# genuinely new interrogative clause — a bare "and" ("cats and dogs") must not
# split, or every noun phrase gets shredded.
_SPLIT_RE = re.compile(
    r"\s*(?:,\s*)?\b(?:and|also|plus)\b\s+(?=(?:what|which|who|whose|when|where|why|"
    r"how|is|are|was|were|do|does|did|can|could|should|would|will|has|have)\b)",
    re.I,
)
_HARD_SPLIT_RE = re.compile(r"\s*[?;]\s*")

_FILTER_RE = re.compile(r"\b(after|before|in|from|type):(\S+)", re.I)
_QUOTED_RE = re.compile(r'"([^"]+)"')
_NEGATED_RE = re.compile(r"(?:^|\s)-(\w[\w'-]*)")

# Questions ABOUT THE DOCUMENT AS A WHOLE rather than about a span inside it:
# "what is this book about", "who wrote this", "summarize this page", "where is
# it set". These are the ones ordinary retrieval gets exactly backwards — BM25
# on "book" in a novel retrieves every passage that happens to contain the word
# "book", while the title page (which actually answers it) ranks nowhere. They
# need a structural sample of the document instead; see engine._global_picks.
_GLOBAL_SUBJECT = (r"(?:this|the|it)\s+"
                   r"(?:book|document|doc|page|article|paper|pdf|file|text|story|"
                   r"novel|essay|report|chapter|thread|post|site|website)")
_GLOBAL_RE = re.compile(
    r"(?:"
    # "what is this book about", "who wrote this document", "when is it set"
    rf"\b{_GLOBAL_SUBJECT}\b"
    # bare demonstratives: "what is this about", "what's this about"
    r"|^\s*(?:what(?:'s| is| are)?|who(?:'s| is)?|where|when)\b[^?]{0,24}\bthis\b\s*\??$"
    # setting/plot asks that refer to the work as a whole with a bare "this"/"it":
    # "where does this take place", "when is it set", "what happens in this".
    # These read as span questions to a word matcher, which is precisely why
    # they failed — the setting of a book is a property of the book, and front
    # matter plus a spread is far better evidence than chunks sharing a word.
    r"|\b(?:where|when)\s+(?:does|do|did|is|was|are|were)\s+(?:this|it)\b"
    r"|\b(?:this|it)\s+(?:take|takes)\s+place\b"
    r"|\b(?:is|was)\s+(?:this|it)\s+set\b"
    r"|\bwhat\s+(?:happens|happened)\s+in\s+(?:this|it)\b"
    r"|\bwhat(?:'s| is)\s+(?:this|it)\b"
    # summarization / gist asks, with or without an object
    r"|\b(?:summar\w*|tl;?dr|gist|overview|synopsis|abstract)\b"
    # "the main point/argument/theme/thesis" of the whole thing
    r"|\bmain\s+(?:point|idea|argument|theme|thesis|takeaway|topic)\b"
    # authorship
    r"|\bwho\s+(?:wrote|is\s+the\s+author|are\s+the\s+authors)\b"
    r"|\bauthor\s+of\b"
    r")", re.I)


@dataclass
class ParsedQuery:
    raw: str
    # What actually gets embedded / BM25'd, filters and negations stripped out.
    clean: str
    subqueries: list[str]
    mode: str                       # 'answer' | 'navigate' | 'literal'
    exclude: list[str] = field(default_factory=list)
    filters: dict[str, str] = field(default_factory=dict)
    exact: list[str] = field(default_factory=list)
    # 'span'   — the answer lives in some specific passage; find it.
    # 'global' — the question is about the document as a whole, so the right
    #            evidence is a structural sample (front matter + a spread),
    #            not the chunks that happen to share words with the query.
    scope: str = "span"

    def to_dict(self) -> dict:
        return {"raw": self.raw, "clean": self.clean, "subqueries": self.subqueries,
                "mode": self.mode, "exclude": self.exclude, "filters": self.filters,
                "exact": self.exact, "scope": self.scope}


def _decompose(text: str) -> list[str]:
    """Split a multi-part question into independently retrievable sub-queries."""
    parts: list[str] = []
    for hard in _HARD_SPLIT_RE.split(text):
        if not hard.strip():
            continue
        parts.extend(p for p in _SPLIT_RE.split(hard) if p.strip())
    parts = [p.strip(" ,?.") for p in parts]
    parts = [p for p in parts if len(p.split()) >= 2]
    # One part means it was never multi-part; don't hand back a copy of itself.
    return parts if len(parts) > 1 else []


def parse(raw: str) -> ParsedQuery:
    text = raw.strip()
    if not text:
        return ParsedQuery(raw=raw, clean="", subqueries=[], mode="navigate")

    exact = _QUOTED_RE.findall(text)
    filters = {k.lower(): v for k, v in _FILTER_RE.findall(text)}
    exclude = _NEGATED_RE.findall(text)

    clean = _QUOTED_RE.sub(lambda m: m.group(1), text)
    clean = _FILTER_RE.sub("", clean)
    clean = _NEGATED_RE.sub("", clean)
    clean = re.sub(r"\s{2,}", " ", clean).strip()

    if text.startswith("regex:"):
        return ParsedQuery(raw=raw, clean=text[6:].strip(), subqueries=[],
                           mode="literal", exclude=exclude, filters=filters,
                           exact=exact)
    # A fully-quoted query is an explicit demand for exactness — honor it and
    # stay out of the way.
    if exact and not clean.replace(exact[0], "").strip():
        return ParsedQuery(raw=raw, clean=exact[0], subqueries=[], mode="literal",
                           exclude=exclude, filters=filters, exact=exact)

    is_question = bool(_QUESTION_RE.search(clean)) or clean.endswith("?")
    mode = "answer" if is_question else "navigate"
    scope = "global" if _GLOBAL_RE.search(clean) else "span"
    # A document-level ask is always an answer, even phrased as a bare noun
    # ("summary", "overview") that the interrogative test would call navigate.
    if scope == "global":
        mode = "answer"
    subs = _decompose(clean) if is_question else []
    return ParsedQuery(raw=raw, clean=clean, subqueries=subs, mode=mode,
                       exclude=exclude, filters=filters, exact=exact, scope=scope)


def retrieval_queries(pq: ParsedQuery) -> list[str]:
    """What to actually embed: the sub-queries if it decomposed, else the whole
    thing. The full query is kept alongside sub-queries so a chunk answering the
    question as a whole isn't lost."""
    if pq.subqueries:
        return [pq.clean, *pq.subqueries]
    return [pq.clean] if pq.clean else []
