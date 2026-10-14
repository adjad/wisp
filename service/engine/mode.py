"""Which inference engine Wisp talks to: the user's oMLX.app (default) or its own bundled one.

Resolved ONCE at import, like the other `.moe` constants: switching mode needs a
backend restart, and the config module needs the answer before it defines the
settings paths. The default is "external-omlx" so nothing changes for anyone who
has not opted in.
"""
from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path

from service.paths import MOE_DIR

EXTERNAL = "external-omlx"
BUNDLED = "bundled"
_MODES = (EXTERNAL, BUNDLED)


def _version(text: str) -> tuple[int, ...]:
    return tuple((list(map(int, re.findall(r"\d+", text))) + [0, 0, 0])[:3])


def unsupported_reason(root: Path | None = None, mac_version: str | None = None) -> str | None:
    """Why the bundled engine cannot run on this Mac, else None.

    The engine's own minimum macOS comes from the ENGINE.json written at packaging time
    (MLX targets a newer macOS than the app). A source checkout has no ENGINE.json and is
    not gated. Used to fall back to the external engine instead of failing at launch."""
    document = (root or engine_code_root()) / "ENGINE.json"
    try:
        needed = str(json.loads(document.read_text()).get("minimum_macos", ""))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError):
        return "The bundled engine's ENGINE.json is unreadable"
    current = mac_version if mac_version is not None else platform.mac_ver()[0]
    if needed and _version(current) < _version(needed):
        return f"The bundled engine needs macOS {needed} or newer (this Mac has {current or 'unknown'})"
    return None


def _configured_mode() -> str:
    value = os.environ.get("WISP_ENGINE_MODE", "").strip()
    if not value:
        try:
            import yaml
            doc = yaml.safe_load((MOE_DIR / "config.yaml").read_text()) or {}
            value = str((doc.get("engine") or {}).get("mode", "")).strip()
        except Exception:  # noqa: BLE001 — an unreadable overlay means the default
            value = ""
    mode = value if value in _MODES else EXTERNAL
    if mode == BUNDLED and unsupported_reason() is not None:
        return EXTERNAL
    return mode



def is_bundled() -> bool:
    return MODE == BUNDLED


def engine_data_dir() -> Path:
    """Private engine state; follows WISP_HOME so the sandbox gets its own engine."""
    return MOE_DIR / "engine"


def engine_code_root() -> Path:
    """Read-only engine code: WISP_ENGINE_ROOT, else `engine/` beside the service package."""
    override = os.environ.get("WISP_ENGINE_ROOT", "").strip()
    # Resolved: attribution refuses an engine root reached through a symlink.
    return (Path(override).expanduser() if override else Path(__file__).parents[2] / "engine").resolve()


def bundled_base_path() -> Path | None:
    """Where the bundled engine keeps settings.json and model_settings.json, else None."""
    return engine_data_dir() / "base" if is_bundled() else None


MODE = _configured_mode()   # last: it needs the helpers above
