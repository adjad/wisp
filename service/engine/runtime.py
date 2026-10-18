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


def reap_orphan() -> bool:
    """Stop an engine a previous backend left running. Safe in any mode: only the process
    recorded in this data dir's engine.pid, and only if it is still the Wisp-launched engine."""
    from .manager import reap_orphan as _reap
    if _manager is not None and _manager.running():
        return False
    return _reap(EngineLayout(engine_code_root(), engine_data_dir()))


def stop_if_running(*, final: bool = False) -> bool:
    """Stop the bundled engine; never touches a user's oMLX. True if one was stopped.

    `final` (Wisp is quitting) also keeps it stopped: a model call still in flight
    cannot launch it again in a backend about to exit."""
    if _manager is None:
        return False
    was_running = _manager.running()
    if final:
        _manager.close()
    elif was_running:
        _manager.stop()
    return was_running


__all__ = ["is_bundled", "manager", "reap_orphan", "stop_if_running"]
