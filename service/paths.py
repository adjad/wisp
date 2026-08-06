"""Where Wisp keeps its state on disk.

Everything Wisp persists — sessions, facts, the profile, the assistant store,
cached Mail/Messages/Notes, skills, grants, the audit log — lives under a single
directory, `~/.moe` by default.

`WISP_HOME` overrides it. That exists for the same reasons the Air node has
`WISPAIR_HOME` (see air/wispair/config.py): tests can run against a scratch
directory instead of the developer's real assistant state, and a second
instance can be pointed somewhere else without editing source. Normal runs set
nothing and get `~/.moe`.

This module deliberately imports nothing from the rest of `service` — it is the
bottom of the import graph, and `service.config` depends on it.
"""
from __future__ import annotations

import os
from pathlib import Path

_override = os.environ.get("WISP_HOME", "").strip()
STATE_DIR: Path = Path(_override).expanduser() if _override else Path.home() / ".moe"


def state_path(*parts: str) -> Path:
    """A path inside the state directory, e.g. state_path("cache")."""
    return STATE_DIR.joinpath(*parts)
