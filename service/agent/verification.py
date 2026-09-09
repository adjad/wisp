"""Check delivery claims against this turn's receipts, independent of routing.

An absent action is as important as a failed one. Keep this separate from the
router's obligations: follow-ups and read-only routes also need verification.
"""
from __future__ import annotations

import re


def verify_delivery_claims(text: str, outcomes: list) -> str:
    # Inspect declarative status sentences, not quoted evidence, questions,
    # instructions, negations, or future intentions.
    patterns = {
        "sent": r"(?:sent|delivered|forwarded)",
        "scheduled": r"(?:scheduled|queued)",
    }
    claims = set()
    for line in text.splitlines():
        if line.lstrip().startswith(('>', '"', '`')):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = re.sub(r"^\s*(?:done|okay|ok)\s*[—–,:-]\s*", "", sentence, flags=re.I)
            if '?' in sentence or re.search(
                r"\b(?:not|never|no|haven't|hasn't|didn't|wasn't|isn't|couldn't|"
                r"will|would|should|can|please)\b", sentence, re.I):
                continue
            for effect, verb in patterns.items():
                if effect == 'scheduled' and not re.search(
                        r"\b(?:message|email|text|report|summary|it|this|that)\b", sentence, re.I):
                    continue
                if re.search(
                    rf"^\s*(?:\*\*)?(?:(?:I(?:'ve| have)?\s+(?:successfully\s+)?{verb})|"
                    rf"(?:(?:the|your|a)\s+)?(?:message|email|text|report|summary|draft)\s+"
                    rf"(?:(?:has been|was|is)\s+)?(?:successfully\s+)?{verb})\b",
                    sentence, re.I):
                    claims.add(effect)
    successful = {outcome.effect for _, outcome in outcomes if outcome.status == "succeeded"}
    missing = claims - successful
    if not missing:
        return text
    receipts = [outcome.text for _, outcome in outcomes
                if outcome.effect in {"sent", "scheduled", "drafted"}
                and outcome.status == "succeeded"]
    if receipts:
        return "\n".join(receipts)
    return "I haven't sent or scheduled a message in this turn."
