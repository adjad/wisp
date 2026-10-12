"""The single place ~/.moe is computed.

WISP_HOME redirects Wisp's OWN state (commitments, sessions, facts, caches,
grants, skills, config overlay) somewhere else — the sandbox uses it to get a
completely separate world without touching the user's real data.

Deliberately NOT $HOME: overriding HOME would also move ~/.omlx/settings.json
and ~/.omlx/model_settings.json (service/config/__init__.py), which is where
the model roster and the user's starred favorites live — the backend would
boot with no models.

Resolved once at import, matching the import-time constants it feeds.
"""
from __future__ import annotations

import os
from pathlib import Path


def moe_dir() -> Path:
    override = os.environ.get("WISP_HOME")
    return Path(override).expanduser() if override else Path.home() / ".moe"


MOE_DIR = moe_dir()


# The Aria demo (WISP_ARIA_DEMO=1, Intel v5) and the founder demo
# (WISP_FOUNDER_DEMO=1) answer from fictional data and learn a synthetic
# persona. The v5 app switches between real mode and the demo at runtime on
# the same WISP_HOME, so everything those modes keep — conversations,
# remembered facts, commitments and assistant events, scheduled sends,
# standing grants, the brief ledger, behavior learning, the synced-content
# caches and the Aria demo's synthetic world — lives in its own subdirectory
# while a demo runs. Only the config overlay and identity stay shared.
# Resolved once at import, like MOE_DIR.
DEMO_STATE_SUBDIR = "aria-demo"


def state_dir() -> Path:
    base = moe_dir()
    if os.environ.get("WISP_ARIA_DEMO") == "1" or os.environ.get("WISP_FOUNDER_DEMO") == "1":
        return base / DEMO_STATE_SUBDIR
    return base


STATE_DIR = state_dir()
if STATE_DIR != MOE_DIR:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
