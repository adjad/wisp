"""Near-miss titles for a by-title write that matched nothing.

"cancel the yoga one", "move the dentist thing", "I watered the plants": the
user's words share a stem with the real title but are not a substring of it.
The tool still refuses (no fuzzy write ever runs); it names the closest titles
so the model can call again with the exact one, or ask.
"""
from __future__ import annotations

import re
from typing import Iterable

_STOP = frozenset("the a an my to of for and one thing that this with at on in".split())


def _stems(text: str) -> set[str]:
    return {w[:4] for w in re.findall(r"[a-z0-9]+", (text or "").casefold())
            if len(w) >= 3 and w not in _STOP}


def similar_titles(query: str, titles: Iterable[str], limit: int = 3) -> list[str]:
    """Distinct titles sharing a word stem with `query`, most shared first."""
    want = _stems(query)
    if not want:
        return []
    scored: dict[str, int] = {}
    for title in titles:
        if title and (score := len(want & _stems(title))):
            scored[title] = max(score, scored.get(title, 0))
    return sorted(scored, key=lambda t: (-scored[t], t))[:limit]


def near_miss_note(query: str, titles: Iterable[str]) -> str:
    """" Closest titles: “Yoga class”. …" or "" when nothing is close."""
    near = similar_titles(query, titles)
    if not near:
        return ""
    return (" Closest titles: " + ", ".join(f"“{t}”" for t in near)
            + ". Nothing was changed; if the user meant one of these, call again with that exact title.")
