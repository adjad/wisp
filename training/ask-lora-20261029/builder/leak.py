"""Eval-leak check: a training prompt must not repeat, or nearly repeat, any
question of the 2026-10-29 Ask eval (eval_questions.jsonl).

Weekday and month names and numbers are folded to placeholders first, so a
template ("anything to prep for Friday?") counts as the same question for any
day. A prompt leaks when its folded form equals an eval question's, or their
character-trigram Jaccard similarity is at least THRESHOLD."""
from __future__ import annotations

import json
import os
import re

DATASET_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THRESHOLD = 0.7

_DAYS = r"(?:mon|tues|tue|wednes|wed|thurs|thu|fri|satur|sat|sun)(?:day)?s?"
_MONTHS = (r"(?:january|february|march|april|may|june|july|august|september|october|november|december|"
           r"jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)")


def fold(text: str) -> str:
    s = text.lower().replace("’", "'").replace("'", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(rf"\b{_DAYS}\b", " wd ", s)
    s = re.sub(rf"\b{_MONTHS}\b", " mo ", s)
    s = re.sub(r"\b\d+(?:st|nd|rd|th)?\b", " n ", s)
    return re.sub(r"\s+", " ", s).strip()


def trigrams(text: str) -> set[str]:
    s = f" {fold(text)} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def load_eval(path: str | None = None) -> list[dict]:
    path = path or os.path.join(DATASET_DIR, "eval_questions.jsonl")
    out = []
    for line in open(path):
        q = json.loads(line)
        out.append({"id": q["id"], "prompt": q["prompt"], "fold": fold(q["prompt"]), "tri": trigrams(q["prompt"])})
    return out


class LeakIndex:
    def __init__(self, questions: list[dict] | None = None):
        self.questions = questions if questions is not None else load_eval()
        self.folds = {q["fold"]: q["id"] for q in self.questions}

    def nearest(self, prompt: str) -> tuple[float, dict | None]:
        f, t = fold(prompt), trigrams(prompt)
        if f in self.folds:
            return 1.0, next(q for q in self.questions if q["fold"] == f)
        best, who = 0.0, None
        for q in self.questions:
            j = len(t & q["tri"]) / max(1, len(t | q["tri"]))
            if j > best:
                best, who = j, q
        return best, who

    def leaks(self, prompt: str) -> tuple[bool, float, dict | None]:
        score, q = self.nearest(prompt)
        return score >= THRESHOLD, score, q
