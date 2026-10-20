"""Is this sentence a REQUEST, or does it only mention one?

The deterministic fast paths (direct device calls, typed reminder/message tasks,
structured reads) recognize intent by keyword. A keyword cannot tell these apart:

    lock my screen                         -> an instruction
    Do not lock my screen.                 -> the opposite instruction
    Explain the phrase "lock my screen".   -> a question about words
    Say "remind me to call Mom tomorrow".  -> a request to say something

Running the first and acting on the other three locks the screen, or writes a
reminder the user never asked for. A fast path may run only when the sentence is
plainly asking for the thing; everything else goes to the model, which can read
the whole sentence. This module is the single place that decides that, so the
fast paths share one definition instead of each growing its own keyword list.

Deliberately conservative in one direction: a sentence that is misjudged as
"not plain" merely takes the slower model route and still gets answered, while a
sentence misjudged as plain runs an action the user did not ask for.

Dependency-free so the router, the task engine and the workflow compiler can all
import it without importing one another.
"""
from __future__ import annotations

import re

# Spans the user QUOTED. Straight and curly double quotes and backticks always
# count; a single-quoted span counts only at word edges so apostrophes ("don't",
# "Sam's") never open one.
_QUOTED = re.compile(
    r'"(?:\\.|[^"\\])*"|“[^”]*”|`[^`]*`|'
    r"(?<!\w)'(?:\\.|[^'\\]|(?<=\w)'(?=\w)){2,}'(?!\w)|"
    r"(?<!\w)‘(?:[^’]|(?<=\w)’(?=\w)){2,}’(?!\w)")

_LEAD = r"^[\W_]*(?:(?:please|hey|ok(?:ay)?|wisp|and|but|also|actually|now|just)\b[\s,]*)*"

# "Do not lock my screen", "don't email Sam", "never delete that", "no need to
# send it". The whole request is a prohibition. ("Stop the music" is an action,
# not a prohibition, so "stop" is deliberately not a cue.) "Don't forget
# to…" and "don't let me forget…" ask for the opposite (a reminder), and
# "don't worry/hesitate" carry no action at all.
_PROHIBITION = re.compile(
    _LEAD + r"(?:do\s*n[o']?t|don[’']?t|never|no\s+need\s+to|there(?:[’']s| is)\s+no\s+need\s+to)\b"
    r"(?!\s+(?:forget|let\s+me\s+forget|hesitate|worry|be\s+shy|mind)\b)", re.I)

# The sentence is ABOUT words, not an instruction to act on them.
_MENTION_VERB = re.compile(
    _LEAD + r"(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:explain|define|translate|spell|pronounce|say|repeat|echo|quote|type(?:\s+out)?"
    r"|write\s+out|read(?:\s+aloud)?\s+(?:the|this|that)\s+(?:word|phrase|sentence|text|line|string)"
    r"|(?:tell\s+me\s+)?what\s+(?:does|do)\b|tell\s+me\s+what\b"
    r"|what\s+is\s+(?:the\s+)?meaning\s+of)\b", re.I)
_WORDS_NOUN = re.compile(r"\b(?:the\s+)?(?:word|words|phrase|sentence|string|command|expression|term)\b", re.I)
# `Say "hi" to Mom` is a message to send, not a mention.
_RECIPIENT_AFTER_QUOTE = re.compile(r"\b(?:to|for)\s+(?!me\b|us\b|myself\b)[\w@.-]+", re.I)

# A closed explanation is self-contained. Keep its quoted bytes opaque and
# consume the entire outer grammar before granting the tool-free contract.
_EXPLANATION_LEAD = (_LEAD + r"(?:(?:can|could|would|will)\s+you\s+)?"
                     r"(?:please\s+)?")
_TERM_LABEL = r"(?:(?:the|this|that)\s+)?(?:word|words|phrase|sentence|command|expression|term)\s*:?\s*"
_EXPLANATION_FRAMES = tuple(re.compile(_EXPLANATION_LEAD + frame + r"(?:,?\s+please)?[.!?\s]*", re.I) for frame in (
    r"(?:tell\s+me\s+)?what\s+(?:does|do)\s+(?P<term>.+?)\s+mean",
    r"what\s+is\s+(?:the\s+)?meaning\s+of\s+(?P<term>.+?)",
    r"(?:explain|help\s+me\s+understand)\s+what\s+(?P<term>.+?)\s+means?",
    r"(?:explain|define|help\s+me\s+understand)\s+(?P<term>.+?)",
    r"what\s+is\s+the\s+difference\s+between\s+(?P<left>.+?)\s+and\s+(?P<right>.+?)",
))
_PERSONAL_OR_OPEN_TERM = re.compile(
    r"\b(?:my|our|your|his|her|their|mine|ours|yours|me|us|this|that|these|those|"
    r"latest|last|recent|selected|current|first|previous|next|today|tomorrow|yesterday|"
    r"from|to|with|by|in|on|at|where|when|who|whose|which|why|how|"
    r"and|then|but|also|without|except|excluding|after|before|about|"
    r"sent|received|said|says|saying|wrote|written|got|happened|"
    r"did|does|do|has|have|had|was|were|is|are|can|could|would|will)\b|['’]s\b", re.I)


def explanation_terms(text: str) -> tuple[str, ...] | None:
    """A fully consumed explanation of language or a generic term.

    This is intentionally narrower than every request to explain something:
    a selected/private record ("explain my latest email"), unknown punctuation
    or another action clause keeps its ordinary source/action authority.
    Callers must preserve the original request when applying this contract.
    """
    masked = _QUOTED.sub("__literal__", text or "")

    def term(value: str) -> bool:
        value = re.sub(r"^" + _TERM_LABEL, "", value.strip(), count=1, flags=re.I)
        if value == "__literal__":
            return True
        value = re.sub(r"^(?:a|an|the)\s+", "", value, count=1, flags=re.I)
        words = re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", value)
        return bool(0 < len(words) <= 8
                    and re.fullmatch(r"[A-Za-z]+(?:[-'][A-Za-z]+)*(?:\s+[A-Za-z]+(?:[-'][A-Za-z]+)*)*", value)
                    and not _PERSONAL_OR_OPEN_TERM.search(value))

    for frame in _EXPLANATION_FRAMES:
        match = frame.fullmatch(masked)
        if match and all(term(value) for value in match.groupdict().values()):
            return tuple(re.sub(r'^' + _TERM_LABEL, '', value.strip(), count=1, flags=re.I)
                         for value in match.groupdict().values())
    return None


def is_complete_explanation(text: str) -> bool:
    return explanation_terms(text) is not None


_HISTORICAL_RECIPIENT = (
    r"(?:[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}|"
    r"\+?\d[\d(). -]{6,}\d|"
    r"(?!(?:and|then|but|also)\b)[A-Za-z][A-Za-z'’-]*"
    r"(?:\s+(?!(?:and|then|but|also)\b)[A-Za-z][A-Za-z'’-]*){0,2})")
_RETROSPECTIVE_COMMUNICATION = re.compile(
    r"\s*\**[ \t]*(?:"
    # Existing whole-question family, shared with every admission boundary.
    r"(?:what\s+(?:did|has|had)|did)\s+(?:my\s+)?"
    r"[A-Za-z][A-Za-z'’-]*(?:\s+[A-Za-z][A-Za-z'’-]*){0,2}\s+"
    r"(?:text(?:ed)?|e-?mail(?:ed)?|message[ds]?|send|sent|write|wrote|say|said|tell|told)"
    r"(?:\s+(?:me|to\s+me|back|earlier|today|yesterday|last\s+night|this\s+morning|recently|lately))*"
    r"|what\s+(?:(?:messages?|texts?|iMessages?|e-?mails?)\s+)?"
    r"did\s+I\s+(?:send|write)"
    r"(?:\s+to\s+" + _HISTORICAL_RECIPIENT + r")?"
    r"(?:\s+(?:yesterday|today|last\s+(?:night|week|month|year)|this\s+morning|earlier|recently|lately))?"
    r")[.!?\s]*\**[ \t]*", re.I)


def is_retrospective_communication_question(text: str) -> bool:
    """A whole past-send question, never current delivery or authored data.

    This finite grammar consumes every byte, including optional leading
    emphasis and the bounded historical recipient/time operands. An authored
    body, unsupported qualifier or independent clause does not match and
    retains its existing owner and source policy.
    """
    return _RETROSPECTIVE_COMMUNICATION.fullmatch(text or "") is not None


_CLAUSE_COMMAND = (r"(?:(?:please|and|then|also|but)\s+)*(?:(?:can|could|would|will)\s+you\s+)?"
                   r"(?:explain|define|help|what|show|list|check|read|search|find|send|"
                   r"text|message|e-?mail|share|forward|draft|compose|write|delete|remove|"
                   r"clear|complete|finish|mark|update|rename|reschedule|move|remind|"
                   r"create|add|set|lock|open)\b")
_EXPLANATION_BOUNDARY = re.compile(
    r"[;\n]+|[.!?](?=\s|$)|\s+(?:and(?:\s+then)?|then|but|also)\s+(?=" + _CLAUSE_COMMAND + r")|"
    r",\s*(?=" + _CLAUSE_COMMAND + r")", re.I)
_AUTHORED_CLAUSE_OWNER = re.compile(
    _LEAD + r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:"
    r"remind\s+me\b|(?:remember|don['’]?t\s+forget)\s+to\b|"
    r"(?:add|create|set|make|schedule)\s+(?:me\s+)?(?:(?:an?|the|my)\s+)?(?:reminders?|alarm)\b|"
    r"(?:text|message|imessage|e-?mail|send|reply|respond)\b[^;?\n]{0,200}?"
    r"(?:\bsaying\b|\bthat\s+says\b|\band\s+say\b|\bto\s+say\b|\bthat\b|:)\s+|"
    r"(?:save|store|write|draft|update|edit|replace)\b[^;?\n]{0,160}"
    r"\b(?:knowledge|style|follow-up|note|file)\b)", re.I)
_DEPENDENT_EXPLANATION = re.compile(
    _EXPLANATION_LEAD + r"explain\s+(?:it|(?:the\s+)?(?:findings|results)|what\s+it\s+means)"
    r"(?:,?\s+please)?[.!?\s]*", re.I)


def explanation_remainder(text: str) -> str | None:
    """Current clauses after removing independently complete explanations.

    None means no clause was recognized. Only clear outside-quote boundaries
    split clauses; original quoted bytes, addresses and authored slots stay
    intact. This is an authority projection, never replacement user content.
    """
    if _AUTHORED_CLAUSE_OWNER.match(text or ""):
        return None
    if _DEPENDENT_EXPLANATION.fullmatch(text or ""):
        return None  # Presentation belongs to its existing source/result owner.
    if is_complete_explanation(text):
        return ""
    opaque = list(text or "")
    for match in _QUOTED.finditer(text or ""):
        opaque[match.start():match.end()] = "x" * (match.end() - match.start())
    projected, explained, actual_before, cursor, prior_boundary = list(text), False, False, 0, 0
    boundaries = list(_EXPLANATION_BOUNDARY.finditer("".join(opaque)))
    if not boundaries:
        return None
    for end, next_start in [(m.start(), m.end()) for m in boundaries] + [(len(text), len(text))]:
        tail = re.sub(r"^(?:(?:and|then|also|but)\s+)+", "", text[cursor:].strip(), flags=re.I)
        if _AUTHORED_CLAUSE_OWNER.match(tail):
            break  # An explicit body/title owner consumes its literal tail.
        clause = text[cursor:end].strip()
        clause = re.sub(r"^(?:(?:and|then|also|but)\s+)+", "", clause, flags=re.I)
        if not clause:
            cursor, prior_boundary = next_start, end
            continue
        if is_complete_explanation(clause) and not _DEPENDENT_EXPLANATION.fullmatch(clause):
            explained = True
            start = prior_boundary if actual_before else 0
            stop = end if actual_before and text[next_start:].strip() else next_start
            projected[start:stop] = " " * (stop - start)
        else:
            actual_before = True
        cursor, prior_boundary = next_start, end
    if not explained:
        return None
    result = "".join(projected).lstrip()
    return re.sub(r"^(?:(?:and|then|also|but)\s+)+", "", result, flags=re.I)


def quoted_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _QUOTED.finditer(text or "")]


def mask_quoted(text: str) -> str:
    """The sentence with every quoted span replaced by an empty quotation, so
    pattern matching sees the instruction and not the words being discussed."""
    return _QUOTED.sub('"…"', text or "")


def is_prohibition(text: str) -> bool:
    """The whole request is "do NOT do X". Only a leading prohibition counts: a
    mid-sentence exclusion ("my calendar, don't include reminders") is a scoped
    read and is handled by the source-exclusion logic, not here."""
    match = _PROHIBITION.match(text or "")
    if not match:
        return False
    # "Don't search my notes; is there anything about X this week" keeps a real
    # request in a later clause: the negation scopes an exclusion, not the whole
    # sentence, and the source-exclusion logic owns that case. A trailing
    # pleasantry ("don't lock my screen, thanks") is not a request.
    later = re.split(r"[;.!?,]|\s+(?:but|and|then|instead|just|rather)\s+", text[match.end():])[1:]
    affirmative = re.compile(
        r"^\s*(?:(?:please|just|instead|rather)\s+)*(?:"
        r"is\s+there\b|what(?:[’']s|\s+is|\s+are)\b|"
        r"(?:show|check|list|search|find|send|email|text|message|remind|"
        r"delete|remove|cancel|add|set|create|move|archive|forward|reply|lock|"
        r"read|open|call)\b|tell\s+me\s+(?:my|the|our)\b)", re.I)
    # Reasons ("I am in a call", "it contains private information") and
    # explanations of the forbidden action are not permission to perform it.
    return not any(affirmative.match(piece) for piece in later)


def is_mention(text: str) -> bool:
    """The request talks about a word, phrase or command rather than asking for
    it to happen."""
    text = text or ""
    match = _MENTION_VERB.match(text)
    if not match:
        return False
    quoted = quoted_spans(text)
    if not quoted and not _WORDS_NOUN.search(text):
        return False
    # A later independent request is still an instruction. Quoted content
    # cannot supply this clause's verb, recipient or source.
    if re.search(r"(?:[;.!?\n,]|\b(?:and|then|but|while)\b)\s*"
                 r"(?:(?:and|then|please|also)\s+)*"
                 r"(?:send|text|message|e-?mail|share|forward|draft|compose|write|"
                 r"show|list|read|check|delete|remove|clear|remind|create|update)\b",
                 mask_quoted(text)[match.end():], re.I):
        return False
    if match.group(0).strip().lower().endswith("say"):
        # `Say "hi" to Mom` sends something; `Say "x".` only echoes.
        tail_from = quoted[-1][1] if quoted else match.end()
        if _RECIPIENT_AFTER_QUOTE.search(text[tail_from:]):
            return False
    return True


def deliberate(text: str) -> str | None:
    """Why a deterministic fast path must NOT act on this sentence, or None when
    the sentence is a plain request. Callers hand a non-None sentence to the
    model; they never discard it."""
    if is_prohibition(text):
        return "the request is a prohibition, not an instruction"
    if is_mention(text):
        return "the request talks about words rather than asking for an action"
    return None
