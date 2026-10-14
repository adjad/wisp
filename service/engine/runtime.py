"""The one engine this backend process owns, when running in bundled mode."""
from __future__ import annotations

from .manager import EngineLayout, EngineManager
from .mode import engine_code_root, engine_data_dir, is_bundled

_manager: EngineManager | None = None


def manager() -> EngineManager:
    global _manager
    if _manager is None:
        _manager = EngineManager(EngineLayout(engine_code_root(), engine_data_dir()))
    return _manager


def stop_if_running() -> bool:
    """Stop the bundled engine; never touches a user's oMLX. True if one was stopped."""
    if _manager is None or not _manager.running():
        return False
    _manager.stop()
    return True


__all__ = ["is_bundled", "manager", "stop_if_running"]
