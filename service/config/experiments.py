"""Opt-in experiment switches, for measuring a behavior change before adopting it.

`WISP_EXPERIMENTS` is a comma-separated list of names, read once at import like
the other launch-time flags (model_led's `WISP_MODEL_LED_ROUTING`). Unset means
every experiment is off and Wisp behaves exactly as shipped. The switches exist
so `scripts/eval_arms.py` can start one isolated backend per arm and compare
the arms on the same prompts; they are not a user-facing tuning surface.

Lifecycle: an experiment that measures better becomes a profile default (see
docs/plans/2026-10-13-capable-model-profiles.md) and its switch is deleted; one
that measures worse is deleted with its measurement recorded next to the site
it touched. Either way a switch is temporary.

Nothing here is a safety boundary. A switch may change an output budget or a
think block; it must never change approval, egress or disclosure rules.
"""
from __future__ import annotations

import os

KNOWN: frozenset[str] = frozenset({
    # memory/capture.py — fact extraction thinks (the worker is idle-only, so
    # this costs no live latency); budget 650 -> 1500. UNMEASURED: needs a
    # replay of stored self-fact turns through the extractor, not /agent.
    "capture_think",
    # Retired 2026-10-13 after measurement on Ling-3.0-tiny-Wisp-V3-merged-oQ4e
    # (scripts/eval_arms.py runs 20261013-140051 and -142918):
    #   search_think — correct 33% vs 33% baseline, same cases, slower; the
    #     failures were uncited answers dropped by verify() (fixed there).
    #   narration_think_compound — its condition (two or more tools answered
    #     in one turn) fired on 2 of 177 live turns; nothing to measure.
})


def parse(value: str | None) -> frozenset[str]:
    """Names from an env value. Unknown names raise so a typo fails at startup
    rather than silently measuring the baseline under an experiment's label."""
    names = {n.strip().lower() for n in str(value or "").split(",") if n.strip()}
    unknown = names - KNOWN
    if unknown:
        raise ValueError(f"Unknown WISP_EXPERIMENTS entries: {sorted(unknown)} "
                         f"(known: {sorted(KNOWN)})")
    return frozenset(names)


_ACTIVE: frozenset[str] = parse(os.environ.get("WISP_EXPERIMENTS"))


def experiment(name: str) -> bool:
    """True when `name` was enabled at launch. Unknown names raise: a call site
    checking a switch that no longer exists is a leftover, not a false."""
    if name not in KNOWN:
        raise ValueError(f"Unknown experiment {name!r}")
    return name in _ACTIVE


def active() -> frozenset[str]:
    return _ACTIVE
