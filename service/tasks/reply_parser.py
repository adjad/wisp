"""Bounded reply grammar shared by compilation and source selection.

Quotes are lexical boundaries, including after an optional topic article.
Body introducers are recognized only outside quotes. Temporal recognition is
applied to the resulting delivery spans, never to a complete quoted selector.
Unquoted topics support temporal names (``Friday lunch``, ``September 12``)
and event-clock names (``meeting at 6``). Other temporal suffixes are ambiguous
and stop the reply; quoting the complete topic makes its intent explicit.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


_QUOTES = {'"': '"', "'": "'", '“': '”', '‘': '’'}
_ARTICLES = {"the", "a", "an"}
_BODY_INTROS = (("saying",), ("and", "say"), ("to", "say"), ("that", "says"))
_DAY = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_MONTH = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
          r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_DATE = (rf"\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}[/-]\d{{1,2}}(?:[/-]\d{{2,4}})?|"
         rf"(?:{_MONTH})\s+\d{{1,2}}(?:,?\s+\d{{4}})?|\d{{1,2}}\s+(?:{_MONTH})(?:\s+\d{{4}})?")
_CLOCK = r"\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|noon|midnight"
_TIME = (rf"(?:at|by)\s+(?:{_CLOCK})|\d{{1,2}}:\d{{2}}|\d{{1,2}}\s*[ap]\.?m\.?|"
         rf"(?:on\s+)?(?:{_DATE})|(?:(?:next|this|on)\s+)?(?:{_DAY})|"
         r"today|tomorrow|tonight|later(?:\s+today)?|next\s+week|"
         r"(?:(?:this|next)\s+)?(?:morning|afternoon|evening|night)|noon|midnight|"
         r"in\s+(?:\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|"
         r"fifteen|twenty|thirty|forty|sixty)\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)")
_TIMING = re.compile(rf"(?<!\w)(?:{_TIME})(?!\w)", re.I)
# In delivery spans even unsupported/ill-formed clocks must remain terminal.
_UNRESOLVED = re.compile(
    r"\b(?:at|by)\s+\S+|\b(?:next|this)\s+(?:week|month|year)\b|"
    r"\b(?:after|before|around|approximately)\s+\S+|\b(?:later|sometime|eventually)\b|"
    r"\b(?:at|by|on|in|next)\s*$", re.I)


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int
    quoted: bool = False
    closed: bool = True


def _tokens(text: str) -> list[Token]:
    """Scan words and complete quote spans; apostrophes within words are literal."""
    result = []
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        start = i
        if text[i] in _QUOTES:
            close = _QUOTES[text[i]]
            closed = False
            i += 1
            while i < len(text):
                if text[i] == "\\" and i + 1 < len(text):
                    i += 2
                elif text[i] == close:
                    # A possessive/contraction apostrophe is not a closing quote.
                    if close in {"'", "’"} and i + 1 < len(text) and text[i + 1].isalnum():
                        i += 1
                        continue
                    i += 1
                    closed = True
                    break
                else:
                    i += 1
            result.append(Token(text[start:i], start, i, True, closed))
        else:
            while i < len(text) and not text[i].isspace() and text[i] not in {'"', '“', '‘'}:
                i += 1
            result.append(Token(text[start:i], start, i))
    return result


def _delivery_time(text: str) -> str:
    match = _TIMING.search(text) or _UNRESOLVED.search(text)
    return match.group() if match else ""


@dataclass(frozen=True)
class ReplyParts:
    reference: str
    source: str
    topic: str = ""
    raw_topic: str = ""
    raw_body: str = ""
    delivery_spans: tuple[str, ...] = ()
    uncertain_timing: str = ""
    body_timing: tuple[str, str] | None = None

    @property
    def schedule_requested(self) -> str:
        return self.uncertain_timing or next(
            (when for span in self.delivery_spans if (when := _delivery_time(span))), "")


def parse_reply_reference(reference: str) -> ReplyParts:
    reference = reference.strip()
    tokens = _tokens(reference)
    about = next((i for i, token in enumerate(tokens)
                  if not token.quoted and token.text.casefold() == "about"), None)
    if about is None:
        return ReplyParts(reference, reference, delivery_spans=(_source_delivery_text(reference),))
    source = reference[:tokens[about].start].strip()
    topic_tokens = tokens[about + 1:]
    topic_end = len(reference)
    # Preserve the existing account selector after a topic, but never extract
    # account-like words from inside a quoted topic.
    for i, token in enumerate(topic_tokens):
        if not token.quoted and token.text.casefold() in {"in", "on"}:
            end = next((j for j in range(i + 2, len(topic_tokens))
                        if not topic_tokens[j].quoted and topic_tokens[j].text.casefold().rstrip(".") == "account"), None)
            if end == len(topic_tokens) - 1:
                source += " " + reference[token.start:]
                topic_end = token.start
                topic_tokens = topic_tokens[:i]
                break
    source_delivery = _source_delivery_text(source)
    if topic_tokens and topic_tokens[0].text.casefold() in _ARTICLES:
        topic_tokens = topic_tokens[1:]
    if not topic_tokens:
        return ReplyParts(reference, source, delivery_spans=(source_delivery,))
    raw_topic = reference[topic_tokens[0].start:topic_end].strip()
    first = topic_tokens[0]
    if any(t.quoted and not t.closed for t in topic_tokens):
        # An unmatched quote cannot confer literal-topic status on timing.
        return ReplyParts(reference, source, raw_topic, raw_topic,
                          delivery_spans=(source_delivery,), uncertain_timing=_delivery_time(raw_topic))
    if first.quoted:
        suffix = reference[first.end:topic_end].strip()
        if not suffix or _delivery_time(suffix):
            return ReplyParts(reference, source, first.text[1:-1], first.text,
                              delivery_spans=(source_delivery, suffix))
        # Mixed quoted/unquoted names, e.g. `"tomorrow" project`, are literal
        # names. Keep the quote characters when they are inside the selector.
        return ReplyParts(reference, source, raw_topic, raw_topic, delivery_spans=(source_delivery,))

    # A trailing temporal clause in an unquoted selector has no explicit
    # boundary. Accept only the bounded event-clock name grammar; otherwise
    # retain the complete selector and fail closed on the ambiguous suffix.
    outside = "".join(reference[t.start:t.end] + " " if not t.quoted else " " * (t.end - t.start + 1)
                      for t in topic_tokens)
    temporal = list(_TIMING.finditer(outside))
    uncertain = ""
    if temporal:
        last = temporal[-1]
        prefix, tail = outside[:last.start()].strip(), outside[last.end():].strip(" .,!?")
        if prefix and not tail:
            event_clock = re.fullmatch(r"(?:meeting|lunch|dinner|call|review|appointment|event)\s*", prefix, re.I)
            temporal_prefix = _TIMING.fullmatch(prefix)
            if not ((event_clock or temporal_prefix) and re.fullmatch(rf"at\s+(?:{_CLOCK})", last.group(), re.I)):
                uncertain = last.group()
    if not uncertain and (match := _UNRESOLVED.search(outside)):
        # Recognized embedded clocks belong to the topic; malformed timing
        # does not. No temporal parser guess may authorize an immediate reply.
        if not any(m.start() <= match.start() and m.end() >= match.end() for m in temporal):
            uncertain = match.group()
    return ReplyParts(reference, source, raw_topic, raw_topic,
                      delivery_spans=(source_delivery,), uncertain_timing=uncertain)


def _source_delivery_text(source: str) -> str:
    # Quote-delimited sender/account names are source data. Outside quotes,
    # today/yesterday remain the existing source-date selectors.
    return " ".join(t.text for t in _tokens(source)
                    if (not t.quoted or not t.closed) and t.text.casefold() not in {"today", "yesterday"})


def _body_timing(body: str) -> tuple[str, str] | None:
    """Keep an unquoted terminal time unresolved, including unsupported clocks."""
    tokens = _tokens(body)
    for token in tokens:
        if token.quoted:
            continue
        suffix = body[token.start:].strip(" .!?")
        match = _TIMING.fullmatch(suffix)
        if match or (token.text.casefold() in {"at", "by"} and _UNRESOLVED.fullmatch(suffix)):
            return body[:token.start].strip(), suffix
        # Worded/ill-formed times are not licensed as immediate payloads.
        if token.text.casefold() in {"at", "by"} and re.fullmatch(
                r"(?:at|by)\s+(?:(?:half|quarter)\s+(?:(?:past|to)\s+)?\w+|"
                r"\d[^\s]*(?:\s+or\s+\d[^\s]*)?)", suffix, re.I):
            return body[:token.start].strip(), suffix
    return None


def parse_reply_parts(rest: str) -> ReplyParts:
    tokens = _tokens(rest)
    for i, token in enumerate(tokens):
        for intro in _BODY_INTROS:
            span = tokens[i:i + len(intro)]
            if len(span) == len(intro) and all(not t.quoted for t in span) and tuple(t.text.casefold() for t in span) == intro:
                reference = parse_reply_reference(rest[:token.start])
                body = rest[span[-1].end:].strip()
                body_tokens = _tokens(body)
                delivery = reference.delivery_spans
                uncertain = reference.uncertain_timing
                if body_tokens and body_tokens[0].quoted:
                    first = body_tokens[0]
                    if first.closed:
                        delivery += (body[first.end:],)
                    else:
                        uncertain = uncertain or _delivery_time(body)
                return ReplyParts(reference.reference, reference.source, reference.topic,
                                  reference.raw_topic, body, delivery, uncertain,
                                  None if body_tokens and body_tokens[0].quoted else _body_timing(body))
    return parse_reply_reference(rest)
