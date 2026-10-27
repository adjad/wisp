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


def stop_if_running(*, final: bool = False, grace: float = 10.0) -> bool:
    """Stop the bundled engine; never touches a user's oMLX. True if one was stopped.

    `final` (Wisp is quitting) also keeps it stopped: a model call still in flight
    cannot launch it again in a backend about to exit, even in a backend that never
    used the engine. It also stops an engine a previous backend left behind, which
    holds its models just the same. `grace` is how long the engine gets to leave on
    SIGTERM before it is killed; on quit it has to fit inside the app's wait."""
    if _manager is None and not final:
        return False
    engine = manager()
    was_running = engine.running()
    if final:
        engine.close(grace)
        return engine.reap_orphan() or was_running
    if was_running:
        engine.stop(grace)
    return was_running


__all__ = ["is_bundled", "manager", "reap_orphan", "stop_if_running"]
