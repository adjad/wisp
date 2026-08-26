"""Small deterministic text helpers shared by extraction, coverage, and
citation verification. Kept dependency-free and model-free on purpose: these
functions are the host-side ground truth that model output gets checked
against, so they must not themselves depend on a model call.
"""
from __future__ import annotations

import re

WORD = re.compile(r"[a-z0-9]{3,}", re.I)
NUMBER = re.compile(r"(?<![\w:])\d+(?:\.\d+)?(?:[bkmt%])?", re.I)
CITE = re.compile(r"\[E\s*:?\s*(\d+)\]", re.I)

_STOP = {"what", "when", "where", "which", "with", "from", "that", "this",
         "have", "does", "about", "into", "most", "important", "research"}
REPORT_STOP = {"about", "according", "also", "because", "being", "between",
               "cited", "claim", "claims", "does", "each", "from", "have",
               "include", "includes", "into", "model", "models", "more",
               "only", "report", "source", "sources", "than", "that", "their",
               "these", "they", "this", "those", "using", "while", "with"}


def terms(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text) if w.lower() not in _STOP}


def numbers(text: str) -> set[str]:
    return {n.lower() for n in NUMBER.findall(text)}
