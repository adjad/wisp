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
    r'"[^"\n]*"|“[^”\n]*”|`[^`\n]*`|(?<![\w])\'[^\'\n]{2,}\'(?![\w])')

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
    r"|what\s+(?:does|do)\b|what\s+is\s+(?:the\s+)?meaning\s+of)\b", re.I)
_WORDS_NOUN = re.compile(r"\b(?:the\s+)?(?:word|words|phrase|sentence|string|command|expression|term)\b", re.I)
# `Say "hi" to Mom` is a message to send, not a mention.
_RECIPIENT_AFTER_QUOTE = re.compile(r"\b(?:to|for)\s+(?!me\b|us\b|myself\b)[\w@.-]+", re.I)


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
    return not any(len(re.findall(r"\w+", piece)) >= 2 for piece in later)


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
