"""Which inference engine Wisp talks to: the user's oMLX.app or its bundled one.

Resolved ONCE at import, like the other `.moe` constants: switching mode needs a
backend restart, and the config module needs the answer before it defines the
settings paths. A fresh install prefers the available built-in engine unless
oMLX.app is installed. The router persists that first choice, never this module.
"""
from __future__ import annotations

import json
import os
import platform
import re
from collections.abc import Callable
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
        needed = json.loads(document.read_text()).get("minimum_macos", "")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError, RecursionError):
        return "The bundled engine's ENGINE.json is unreadable"
    if not isinstance(needed, str) or (needed and re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){0,2}", needed) is None):
        return "The bundled engine's macOS requirement is invalid"
    current = mac_version if mac_version is not None else platform.mac_ver()[0]
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){0,2}", current) is None:
        current = ""
    if needed and _version(current) < _version(needed):
        return f"The bundled engine needs macOS {needed} or newer (this Mac has {current or 'unknown'})"
    return None


def _overlay_selection() -> tuple[bool, str | None]:
    """Whether a selection exists, and its valid value. Never expose raw config."""
    try:
        import yaml
        doc = yaml.safe_load((MOE_DIR / "config.yaml").read_text())
        if doc is None:
            doc = {}
        if not isinstance(doc, dict):
            return True, None
        section = doc.get("engine", {})
        if section is None:
            return False, None
        if not isinstance(section, dict):
            return True, None
        if "mode" not in section:
            return False, None
        value = section["mode"]
        return True, value if isinstance(value, str) and value in _MODES else None
    except FileNotFoundError:
        return False, None
    except Exception:  # noqa: BLE001 — do not overwrite unreadable settings
        return True, None


def configured_mode() -> str | None:
    """Saved next-launch choice, falling back to a valid launch override."""
    present, saved = _overlay_selection()
    if present:
        return saved
    value = os.environ.get("WISP_ENGINE_MODE", "").strip()
    return value if value in _MODES else None


def selection_unset() -> bool:
    return "WISP_ENGINE_MODE" not in os.environ and not _overlay_selection()[0]


def external_omlx_installed(present: Callable[[Path], bool] | None = None) -> bool:
    """Presence only: never open the installed app or its settings. Injectable."""
    present = present or Path.exists
    return any(present(path) for path in (Path("/Applications/oMLX.app"),
                                          Path.home() / "Applications" / "oMLX.app"))


def engine_present(root: Path | None = None) -> bool:
    root = root or engine_code_root()
    return (root / "py" / "bin" / "python3").is_file() and (root / "ENGINE.json").is_file()


def _configured_mode(external_installed: Callable[[], bool] | None = None) -> str:
    if selection_unset():
        installed = external_installed or external_omlx_installed
        return (BUNDLED if engine_present() and unsupported_reason() is None
                and not installed() else EXTERNAL)
    value = os.environ.get("WISP_ENGINE_MODE", "").strip() or _overlay_selection()[1]
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
    if override:
        # Resolved: attribution refuses an engine root reached through a symlink.
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    # Source checkout: <repo>/engine. Packaged app: the backend is Resources/backend and the
    # engine is its sibling, Resources/engine.
    for candidate in (here.parents[2] / "engine", here.parents[3] / "engine"):
        if (candidate / "ENGINE.json").is_file():
            return candidate
    return here.parents[2] / "engine"


def bundled_base_path() -> Path | None:
    """Where the bundled engine keeps settings.json and model_settings.json, else None."""
    return engine_data_dir() / "base" if is_bundled() else None


MODE = _configured_mode()   # last: it needs the helpers above
