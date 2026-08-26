"""Citation rendering and the two-pass claim verification contract.

Pass 1 (deterministic, `line_supported`) is the actual safety boundary: it
runs whether or not the model cooperates, and nothing in pass 2 can override
it. Pass 2 (`critic_verify`) is a second, small-model opinion that may soften
or remove a claim already past pass 1 — it is explicitly not allowed to add a
new factual claim, only to label ones the host already extracted verbatim.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from service.research.models import CriticVerdict
from service.research.textutil import CITE, REPORT_STOP, numbers, terms


def line_supported(line: str, rows: list[dict]) -> bool:
    """Conservative lexical/numeric entailment gate over exact passages."""
    content = CITE.sub("", line)
    support = " ".join(
        f"{row.get('quote', '')} {row.get('title', '')} {row.get('published_at', '')}"
        for row in rows).lower()
    # A cited line may not introduce any number absent from its passages or
    # source metadata. This catches the most damaging benchmark/spec errors.
    line_numbers = numbers(content)
    if not line_numbers.issubset(numbers(support)):
        return False
    line_terms = terms(content) - REPORT_STOP
    if not line_terms:
        return True
    overlap = len(line_terms & terms(support)) / len(line_terms)
    return overlap >= 0.30


async def critic_verify(call_json, claims: list[dict]) -> dict[int, str]:
    """One batched model call labeling already-validated claims.

    `claims` is a list of {index, line, quotes: [str]}. Returns {index: label}
    for labels the model actually returned and validated; missing/invalid
    entries are left out, and callers should treat an absent label as
    "supported" (pass 1 already gated the line) rather than fail closed on a
    critic that didn't answer.
    """
    if not claims:
        return {}
    system = (
        "You are a strict fact-checking critic. For each numbered claim, decide whether its "
        "cited quotes support it. Labels: supported, partially_supported, contradicted, unclear. "
        "You may NEVER add a new fact, number, or source — only judge what is given. Return JSON "
        "only: an array of objects with index and label.")
    user = json.dumps({"claims": [
        {"index": c["index"], "claim": c["line"], "quotes": c["quotes"]} for c in claims
    ]}, ensure_ascii=False)
    try:
        raw = await call_json(system, user)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, str] = {}
    if isinstance(raw, list):
        for row in raw:
            try:
                verdict = CriticVerdict.model_validate(row)
            except ValidationError:
                continue
            out[verdict.index] = verdict.label
    return out
