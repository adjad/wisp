"""The Aria demo: a fully synthetic, interactive Wisp for the fictional founder
Aria Solano. On with ``WISP_ARIA_DEMO=1``, which the Intel v5 app passes
together with ``WISP_REMOTE_ONLY=1`` + ``WISP_REMOTE_FULL=1`` when its
"Aria demo mode" toggle is on (remote_policy refuses it in any other launch).

Pieces: data.py (seed loader + fictional-data validation), world.py (the
writable synthetic store), tools.py (synthetic tool implementations and the
dispatch boundary). All demo state lives under service.paths.state_dir().
"""
from __future__ import annotations

import os

ENV = "WISP_ARIA_DEMO"


def env_requested() -> bool:
    """Import-free env check (paths, identity, behavior flags use this)."""
    return os.environ.get(ENV) == "1"


def enabled() -> bool:
    """True only in a validated full remote launch with the Aria demo on."""
    from service.inference.remote_policy import aria_demo_mode
    return aria_demo_mode()
