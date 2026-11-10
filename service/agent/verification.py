"""Check delivery claims against this turn's receipts, independent of routing.

An absent action is as important as a failed one. Keep this separate from the
router's obligations: follow-ups and read-only routes also need verification.
"""
from __future__ import annotations

import re


# What follows the verb when a passive sentence reports someone else's message
# or when it arrived ("The email was sent on Tuesday by Alex.", "…was sent to
# you by Dana.", "…was delivered at 9:02 PM"), not that Wisp sent one.
_REPORTED_FACT = (r"\*{0,2}\s+(?:to\s+(?:you|us)\s+)?"
                  r"(?:(?:by(?!\s+(?:me|wisp)\b)|from|on|at|in|last|yesterday|earlier|today|around|about)\b|\d)")


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
            # "hasn’t" with a typographic apostrophe is the same negation.
            if '?' in sentence or re.search(
                r"\b(?:not|never|no|haven't|hasn't|didn't|wasn't|isn't|couldn't|"
                r"will|would|should|can|please)\b", sentence.replace("\u2019", "'"), re.I):
                continue
            for effect, verb in patterns.items():
                if effect == 'scheduled' and not re.search(
                        r"\b(?:message|email|text|report|summary|it|this|that)\b", sentence, re.I):
                    continue
                # "Scheduled at 9 PM" is how a scheduling claim reads, so only a
                # sent/delivered sentence can be a report about someone else's mail.
                reported = rf"(?!{_REPORTED_FACT})" if effect == "sent" else ""
                if re.search(
                    rf"^\s*(?:\*\*)?(?:(?:I(?:'ve| have)?\s+(?:successfully\s+)?{verb})|"
                    rf"(?:(?:the|your|a)\s+)?(?:message|email|text|report|summary|draft)\s+"
                    rf"(?:(?:has been|was|is)\s+)?(?:successfully\s+)?{verb}\b{reported})\b",
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
