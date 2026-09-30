"""Text shown to a person that started life as text written for the model.

Dependency-free so the agent loop and the workflow executor can share it.
"""
from __future__ import annotations

import re

_GUESS_CLAUSE_RE = re.compile(
    r"[,;\u2014-]?\s*(?:and\s+)?\b(?:do\s+not|don'?t)\s+(?:guess|retry)(?:\s+one|\s+it)?", re.I)
_ASK_FOR_IT_RE = re.compile(r"[\u2014\u2013-]?\s*\bask\s+the\s+user\s+for\s+(?:it|them|that)\b", re.I)
_ASK_USER_RE = re.compile(r"\bask\s+the\s+user\b\s*", re.I)


def user_facing_failure(result: str) -> str:
    """A tool's failure text is written for the MODEL ("...Ask the user for it \u2014
    don't guess."). When Wisp stops and shows it to the person, address them
    instead: drop "don't guess", and turn "ask the user X" into "please tell me X"."""
    text = str(result or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    cleaned = _GUESS_CLAUSE_RE.sub("", text)
    cleaned = _ASK_FOR_IT_RE.sub(". Please tell me the address or number to use", cleaned)
    cleaned = _ASK_USER_RE.sub("please tell me ", cleaned)
    cleaned = re.sub(r"\s+([.,;])", r"\1", cleaned).strip(" ,;\u2014-")
    cleaned = re.sub(r"\.\s*\.", ".", cleaned)
    if not cleaned:
        return str(result or "").strip()
    cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned if cleaned[-1] in ".?!" else cleaned + "."
