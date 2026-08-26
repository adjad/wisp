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
