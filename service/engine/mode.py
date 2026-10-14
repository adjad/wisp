"""Which inference engine Wisp talks to: the user's oMLX.app (default) or its own bundled one.

Resolved ONCE at import, like the other `.moe` constants: switching mode needs a
backend restart, and the config module needs the answer before it defines the
settings paths. The default is "external-omlx" so nothing changes for anyone who
has not opted in.
"""
from __future__ import annotations

import os
from pathlib import Path

from service.paths import MOE_DIR

EXTERNAL = "external-omlx"
BUNDLED = "bundled"
_MODES = (EXTERNAL, BUNDLED)


def _configured_mode() -> str:
    value = os.environ.get("WISP_ENGINE_MODE", "").strip()
    if not value:
        try:
            import yaml
            doc = yaml.safe_load((MOE_DIR / "config.yaml").read_text()) or {}
            value = str((doc.get("engine") or {}).get("mode", "")).strip()
        except Exception:  # noqa: BLE001 — an unreadable overlay means the default
            value = ""
    return value if value in _MODES else EXTERNAL


MODE = _configured_mode()


def is_bundled() -> bool:
    return MODE == BUNDLED


def engine_data_dir() -> Path:
    """Private engine state; follows WISP_HOME so the sandbox gets its own engine."""
    return MOE_DIR / "engine"


def engine_code_root() -> Path:
    """Read-only engine code: WISP_ENGINE_ROOT, else `engine/` beside the service package."""
    override = os.environ.get("WISP_ENGINE_ROOT", "").strip()
    return Path(override).expanduser() if override else Path(__file__).resolve().parents[2] / "engine"


def bundled_base_path() -> Path | None:
    """Where the bundled engine keeps settings.json and model_settings.json, else None."""
    return engine_data_dir() / "base" if is_bundled() else None
