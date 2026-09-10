"""Bounded reply grammar shared by compilation and source selection.

Quotes are lexical boundaries, including after an optional topic article.
Body introducers are recognized only outside quotes. Temporal recognition is
applied to the resulting delivery spans, never to a complete quoted selector.
Unquoted topics support temporal names (``Friday lunch``, ``September 12``)
and event-clock names (``meeting at 6``). Other temporal suffixes are ambiguous
and stop the reply; quoting the complete topic makes its intent explicit.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
    account: str = ""
    sender: str = ""
    day: str = ""
    selector_error: str = ""

    @property
    def hints(self) -> dict[str, str]:
        if self.selector_error:
            return {}  # Never expose partially interpreted source selectors.
        return {key: value for key, value in (('account', self.account), ('sender', self.sender),
                                       ('day', self.day), ('topic', self.topic)) if value}

    @property
    def schedule_requested(self) -> str:
        return self.uncertain_timing or next(
            (when for span in self.delivery_spans if (when := _delivery_time(span))), "")


def _word(token: Token) -> str:
    return token.text.strip(".,;:!?()[]").casefold() if not token.quoted else ""


def _without(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    return text.strip()


def _account_selector(reference: str) -> tuple[str, str, str]:
    """Extract only complete `in/on [the] LABEL account` token sequences.

    A quoted label is exactly one token; a word inside it can never terminate
    the clause. Repeated markers/clauses or incomplete quoted labels fail
    closed instead of returning a usable partial account or sender.
    """
    tokens = _tokens(reference)
    spans: list[tuple[int, int]] = []
    accounts = []
    for i, token in enumerate(tokens):
        if _word(token) not in {"in", "on"} or any(start <= token.start < end for start, end in spans):
            continue
        j = i + 1
        if j < len(tokens) and _word(tokens[j]) == "the":
            j += 1
        if j == len(tokens):
            continue
        label = tokens[j]
        if _word(label) == "account":
            return reference, "", "Specify the account name before “account”."
        end = j + 1
        if label.quoted:
            marker = end < len(tokens) and _word(tokens[end]) == "account"
            if not label.closed or not marker:
                return reference, "", "Quote the complete account name and follow it with “account”."
            account = label.text[1:-1]
        else:
            while end < len(tokens) and _word(tokens[end]) not in {
                    "account", "about", "from", "in", "on", "today", "yesterday"} and not tokens[end].quoted:
                end += 1
            if end >= len(tokens) or _word(tokens[end]) != "account":
                if end < len(tokens) and tokens[end].quoted and any(
                        _word(t) == "account" for t in tokens[end + 1:]):
                    return reference, "", "Quote the complete account name so its boundary is clear."
                continue
            account = reference[label.start:tokens[end].start].strip()
        if not account.strip() or (end + 1 < len(tokens) and _word(tokens[end + 1]) == "account"):
            return reference, "", "Quote the complete account name so its boundary is clear."
        accounts.append(account)
        spans.append((token.start, tokens[end].end))
    if len(accounts) > 1:
        return reference, "", "Specify one Mail account for this reply."
    return _without(reference, spans), accounts[0] if accounts else "", ""


def parse_reply_reference(reference: str) -> ReplyParts:
    original = reference.strip()
    reference, account, error = _account_selector(original)
    parsed = _parse_topic_reference(reference)
    source_tokens = _tokens(parsed.source)
    days = [t for t in source_tokens if _word(t) in {"today", "yesterday"}]
    if len({_word(t) for t in days}) > 1:
        error = error or "Specify one source day for this reply."
    source = _without(parsed.source, [(t.start, t.end) for t in days]).strip(" .,!?:;")
    tokens = _tokens(source)
    # Delivery evidence already lives in parsed.delivery_spans. Do not also
    # attach it to a pending sender, e.g. `from Eve tomorrow` means sender Eve.
    timing = [(m.start(), m.end()) for m in _TIMING.finditer(source)
              if not any(t.quoted and t.start <= m.start() < t.end for t in tokens)]
    source = _without(source, timing).strip(" .,!?:;")
    tokens = _tokens(source)
    from_token = next((t for t in tokens if _word(t) == "from"), None)
    if from_token:
        sender = source[from_token.end:].strip(" .,!?:;")
    elif match := re.match(r"(.+?)[’']s\s+(?:e-?mail|mail)\b", source, re.I):
        sender = match.group(1).strip()
    elif all(_word(t) in {"that", "the", "this", "a", "an", "email", "e-mail", "mail"} for t in tokens):
        sender = ""
    else:
        sender = source
    return replace(parsed, reference=original, account=account, sender=sender,
                   day=_word(days[0]) if days else "", selector_error=error)


def recover_reply_reference(reference: str) -> ReplyParts | None:
    """Recover non-account constraints only across visibly bounded clauses.

    This is recovery state, not a usable source reference: a valid replacement
    account is still required. Never infer the extent of an unclosed quote or
    an unterminated account clause. Removing complete account spans preserves
    the original sender/topic/day regardless of where those spans appeared.
    """
    tokens = _tokens(reference)
    if any(t.quoted and not t.closed for t in tokens):
        return None
    spans: list[tuple[int, int]] = []
    for i, token in enumerate(tokens):
        if _word(token) not in {"in", "on"} or any(start <= token.start < end for start, end in spans):
            continue
        end = i + 1
        while end < len(tokens) and _word(tokens[end]) not in {
                "account", "about", "from", "in", "on", "today", "yesterday"}:
            end += 1
        if end == len(tokens) or _word(tokens[end]) != "account":
            continue  # e.g. a delivery duration; only remove account clauses
        while end + 1 < len(tokens) and _word(tokens[end + 1]) == "account":
            end += 1
        spans.append((token.start, tokens[end].end))
    if not spans:
        return None
    recovered = parse_reply_reference(_without(reference, spans))
    if recovered.selector_error or recovered.account:
        return None
    return recovered


def complete_reply_selector(parts: ReplyParts) -> bool:
    """A full restatement is required when an old boundary cannot be recovered."""
    return bool(not parts.selector_error and parts.account and parts.sender and parts.topic
                and any(_word(t) in {"email", "e-mail"} for t in _tokens(parts.source)))


def delivery_before_invalid_account(reference: str) -> str:
    """Retain delivery intent before an account whose extent is unknowable.

    The prefix is parsed with the same topic boundaries. Neither quoted topic
    dates nor timing-like text inside the broken account label is scanned.
    """
    for token in reversed(_tokens(reference)):
        if _word(token) in {"in", "on"}:
            return parse_reply_reference(reference[:token.start]).schedule_requested
    return ""


def _parse_topic_reference(reference: str) -> ReplyParts:
    reference = reference.strip()
    tokens = _tokens(reference)
    about = next((i for i, token in enumerate(tokens)
                  if not token.quoted and token.text.casefold() == "about"), None)
    if about is None:
        return ReplyParts(reference, reference, delivery_spans=(_source_delivery_text(reference),))
    source = reference[:tokens[about].start].strip()
    topic_tokens = tokens[about + 1:]
    topic_end = len(reference)
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
                    if (not t.quoted or not t.closed) and _word(t) not in {"today", "yesterday"})


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
                return replace(reference, raw_body=body, delivery_spans=delivery, uncertain_timing=uncertain,
                               body_timing=None if body_tokens and body_tokens[0].quoted else _body_timing(body))
    return parse_reply_reference(rest)
