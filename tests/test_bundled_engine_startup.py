"""Backend startup around the bundled engine: leftovers, readiness, and who is asked first.

Found by the release audit of the bundled engine:
  * the engine outlives a killed backend, and the next `ensure_omlx` used to probe it, refuse it as
    "not owned", and never reach the start path that reaps it;
  * readiness used to be polled with the engine API key before anything had proven who was listening.

Uses fakes for the manager and the client; no engine, no model, no network.

    python tests/test_bundled_engine_startup.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def boot() -> tuple:
    home = tempfile.mkdtemp(prefix="wisp-startup-")
    engine = Path(home) / "staged-engine"
    (engine / "py/bin").mkdir(parents=True)
    (engine / "py/bin/python3").write_text("")
    (engine / "ENGINE.json").write_text('{"minimum_macos": "11.0"}')
    os.environ.update(WISP_HOME=home, WISP_ENGINE_MODE="bundled", WISP_ENGINE_ROOT=str(engine))
    from service import main
    from service.engine import runtime
    from service.inference.omlx_client import ModelLoadError
    return main, runtime, ModelLoadError


class FakeClient:
    managed = True

    def __init__(self, error=None, log=None):
        self.error, self.log = error, log if log is not None else []

    async def health(self):
        self.log.append("health")
        if self.error is not None:
            raise self.error
        return {"status": "ok"}

    def invalidate_connections(self):
        self.log.append("invalidate")


class FakeEngine:
    def __init__(self, log, *, live=False, ready=True):
        self.log, self.live, self.ready = log, live, ready

    def incarnation(self):
        return object() if self.live else None

    def start(self):
        self.log.append("start")
        self.live = True

    async def wait_ready(self, timeout):
        self.log.append("wait_ready")
        return self.ready

    def stop(self):
        self.log.append("stop")
        self.live = False


def run(coro):
    return asyncio.run(coro)


def test_leftover_engine_is_not_probed_first() -> None:
    main, runtime, ModelLoadError = boot()
    check("bundled mode is active", main.is_bundled())
    log: list[str] = []
    refused = ModelLoadError("The built-in engine could not be verified")
    main.client = FakeClient(refused, log)
    runtime._manager = FakeEngine(log, live=False)       # nothing launched by this backend
    started = []

    async def fake_ensure():
        started.append(True)

    original = main._ensure_bundled_engine
    main._ensure_bundled_engine = fake_ensure
    try:
        run(main.ensure_omlx())
    finally:
        main._ensure_bundled_engine = original
    check("a leftover engine goes straight to the start path (which reaps it)", started == [True], str(log))
    check("the client never probed the unverified listener", "health" not in log, str(log))


def test_live_engine_is_verified_not_restarted() -> None:
    main, runtime, ModelLoadError = boot()
    log: list[str] = []
    main.client = FakeClient(None, log)
    runtime._manager = FakeEngine(log, live=True)
    started = []

    async def fake_ensure():
        started.append(True)

    original = main._ensure_bundled_engine
    main._ensure_bundled_engine = fake_ensure
    try:
        run(main.ensure_omlx())
    finally:
        main._ensure_bundled_engine = original
    check("a live launched engine that answers is left alone", not started and "health" in log, str(log))


def test_readiness_then_identity() -> None:
    main, runtime, ModelLoadError = boot()
    log: list[str] = []
    main.client = FakeClient(None, log)
    runtime._manager = FakeEngine(log)
    run(main._ensure_bundled_engine())
    check("start, readiness without credentials, then the checked client",
          log.index("start") < log.index("wait_ready") < log.index("health"), str(log))

    log.clear()
    main.client = FakeClient(ModelLoadError("not the engine"), log)
    runtime._manager = FakeEngine(log)
    try:
        run(main._ensure_bundled_engine())
        check("an engine the client refuses is not accepted", False)
    except ModelLoadError:
        check("an engine the client refuses is not accepted", True)
    check("and it is stopped rather than left holding memory", log[-1] == "stop", str(log))

    log.clear()
    main.client = FakeClient(None, log)
    runtime._manager = FakeEngine(log, ready=False)
    try:
        run(main._ensure_bundled_engine())
        check("an engine that never answers fails", False)
    except ModelLoadError:
        check("an engine that never answers fails", "health" not in log and log[-1] == "stop", str(log))


def test_reap_orphan_is_safe_without_a_pid_file() -> None:
    main, runtime, ModelLoadError = boot()
    runtime._manager = None
    check("no engine.pid: nothing to reap, in any mode", runtime.reap_orphan() is False)


def main() -> int:
    for fn in (test_leftover_engine_is_not_probed_first, test_live_engine_is_verified_not_restarted,
               test_readiness_then_identity, test_reap_orphan_is_safe_without_a_pid_file):
        print(fn.__name__)
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
