"""Edge whitespace for the answer the person reads.

A model often opens a reply with "\\n" or "\\n\\n\\n" (Gemma 4 after a tool
result does it routinely), and may end on blank lines. Those show up as a
gap above the first line of the chat bubble. This trims only the two edges of
the answer. Whitespace inside it is never touched, so markdown lists, code
blocks and line breaks survive.

Streamed deltas cannot be trimmed one at a time: "\\n" arrives alone, and the
text that follows decides whether a trailing newline is the end of the answer
or just a break between paragraphs. So whitespace at the end of a delta is
held back and released ahead of the next delta that carries real text. If no
such delta comes, the held whitespace is dropped: that is the trailing trim.
"""
from __future__ import annotations


class AnswerEdgeTrimmer:
    def __init__(self) -> None:
        self.started = False   # real text has already been sent for this answer
        self.held = ""         # whitespace seen after the last real text

    def reset(self) -> None:
        """The answer so far was discarded; the next text starts a new one."""
        self.started = False
        self.held = ""

    def delta(self, text: str) -> str:
        """What to send for one streamed delta ("" means send nothing)."""
        if not text:
            return ""
        body = text.rstrip()
        if not body:
            if self.started:
                self.held += text
            return ""
        lead = self.held if self.started else ""
        if not self.started:
            body = body.lstrip()
        self.started = True
        self.held = text[len(text.rstrip()):]
        return lead + body

    @staticmethod
    def whole(text: str) -> str:
        """A complete answer: both edges trimmed, the middle untouched."""
        return text.strip()
