"""Whether the person's OWN message named themselves as the recipient this turn.

The agent loop calls `mark_turn(text)` once per turn with the person's own
words (never model text, never a tool result). Send tools read `named_self()`.

Only the recipient slot of a send counts: a clause that starts with a send
verb and is aimed at "me"/"myself" ("email myself a test", "send me a text",
"forward this to my email"). "Email Dana and tell her to call me" names Dana:
the "me" there belongs to a later clause that is not a send. Quoted text and
the body after a colon ("Text Dana: email me later") never count.
"""
from __future__ import annotations

import re
from contextvars import ContextVar

_named_self: ContextVar[bool] = ContextVar("wisp_user_named_self", default=False)

_VERB = r"(?:e-?mail|mail|text|i?message|send|forward|dm|ping|cc|bcc)"
_ME = r"(?:me|myself)"
# "my own address", "my email", "my phone number": the person's own handle.
_MINE = (r"(?:my\s+own\s+(?:e-?mail(?:\s+address)?|address|inbox|account|number|phone(?:\s+number)?|cell)"
         r"|my\s+(?:e-?mail(?:\s+address)?|inbox|(?:phone|cell)(?:\s+number)?|number))")
# Polite or connective words a clause may start with before its verb.
_LEAD = (r"^(?:(?:please|pls|hey|hi|ok(?:ay)?|wisp|now|just|also|then|and|so|quickly|"
         r"can|could|would|will|you|go\s+ahead\s+and|i\s+(?:want|need)\s+you\s+to)\b\s*)*")
# "email me …", "text myself …", "send me a text …", "text my own number".
_DIRECT = re.compile(_LEAD + _VERB + r"\s+(?:(?:it|this|that)\s+)?(?:" + _ME + "|" + _MINE + r")\b", re.I)
# "send the notes to me", "forward this to my email": a send clause aimed at
# me, with no "that …"/"saying …"/"tell …" body in between.
_TO_ME = re.compile(
    _LEAD + _VERB + r"\b(?:(?!\b(?:that|saying|says|tell|ask|who|which|because)\b).)*?"
    r"\bto\s+(?:" + _ME + "|" + _MINE + r")\b", re.I)
# Clause boundaries: sentence ends, commas, and "and"/"then"/"but".
_CLAUSES = re.compile(r"[.;!?\n,]+|\s+(?:and|then|but|also)\s+", re.I)


def user_names_self(text: str) -> bool:
    """True when the person's own message names themselves as a send's recipient."""
    from service.utterance_shape import mask_quoted

    plain = " ".join(mask_quoted(text or "").split())
    # A colon after the instruction starts the message body: "Text myself: hi".
    head = plain.split(":", 1)[0]
    if not re.search(rf"\b{_VERB}\b", head, re.I):
        head = plain  # "Hey Wisp: email me …": the colon was not after a send
    return any(_DIRECT.match(c.strip()) or _TO_ME.match(c.strip())
               for c in _CLAUSES.split(head) if c.strip())


_turn_text: ContextVar[str] = ContextVar("wisp_user_turn_text", default="")


def mark_turn(text: str) -> None:
    _named_self.set(bool(user_names_self(text or "")))
    _turn_text.set(text or "")


def turn_text() -> str:
    """The person's own message this turn (never model text)."""
    return _turn_text.get()


def typed_this_turn(handle: str) -> bool:
    """The person's own message this turn contains this address or number."""
    said, want = _turn_text.get().casefold(), (handle or "").strip().casefold()
    if not want:
        return False
    digits = re.sub(r"\D", "", want)
    return want in said or ("@" not in want and len(digits) >= 7 and digits in re.sub(r"\D", "", said))


def named_self() -> bool:
    return _named_self.get()
