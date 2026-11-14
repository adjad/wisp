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


import re as _re

_HEADING = _re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)[ \t]*$")
_HEADING_PREFIX = _re.compile(r"^ {0,3}#{1,6}[ \t]")


def heading_to_label(line: str) -> str:
    """One line: a markdown heading becomes a bold label; anything else is returned as is."""
    m = _HEADING.match(line)
    if not m:
        return line
    text = _re.sub(r"(^|[ \t]+)#+$", "", m.group(1)).strip()
    if not text:
        return ""
    if len(text) >= 4 and text.startswith("**") and text.endswith("**") and "**" not in text[2:-2]:
        return text
    return f"**{text}**"


class HeadingFixer:
    """Ask shows lists and bold labels only, never `#` headings.

    Works on a stream: a line that might be a heading is held until its
    newline, every other line passes through as soon as its first character
    shows it is not one. Fenced code blocks are left alone.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.in_fence = False
        self.line_head = ""      # the start of the current line, for fence detection
        self.held = ""           # held line start / heading line
        self.mode = "start"      # start | heading | pass

    def _end_line(self) -> None:
        if self.line_head.lstrip().startswith(("```", "~~~")):
            self.in_fence = not self.in_fence
        self.line_head = ""
        self.mode = "start"

    def feed(self, text: str) -> str:
        out: list[str] = []
        for ch in text:
            if ch == "\n":
                if self.mode in ("start", "heading"):
                    out.append(heading_to_label(self.held) if not self.in_fence else self.held)
                    self.held = ""
                out.append(ch)
                self._end_line()
                continue
            if len(self.line_head) < 8:
                self.line_head += ch
            if self.mode == "pass":
                out.append(ch)
            elif self.mode == "heading":
                self.held += ch
            else:  # start of a line
                self.held += ch
                if ch == " " and self.held.strip(" ") == "" and len(self.held) <= 3:
                    continue
                if _HEADING_PREFIX.match(self.held) and not self.in_fence:
                    self.mode = "heading"
                elif _re.fullmatch(r" {0,3}#{1,6}", self.held) and not self.in_fence:
                    continue
                else:
                    out.append(self.held)
                    self.held = ""
                    self.mode = "pass"
        return "".join(out)

    def flush(self) -> str:
        if not self.held:
            return ""
        text = self.held if self.in_fence else heading_to_label(self.held)
        self.held = ""
        self.mode = "pass"
        return text

    @staticmethod
    def whole(text: str) -> str:
        """A complete answer with every heading line turned into a bold label."""
        fixer = HeadingFixer()
        return fixer.feed(text) + fixer.flush()
