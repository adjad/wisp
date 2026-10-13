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
    # agent/loop.py — keep the narration step's think block when the model is
    # synthesizing across two or more tool results.
    "narration_think_compound",
    # search/synth.py — Smart Search synthesis thinks, with a budget that can
    # hold the think block and the answer (1800 short / 2400 long-doc).
    "search_think",
    # memory/capture.py — fact extraction thinks (the worker is idle-only, so
    # this costs no live latency); budget 650 -> 1500.
    "capture_think",
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
