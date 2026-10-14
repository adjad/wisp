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
        self.token = object()

    verified = False

    def incarnation(self):
        return self.token if self.live else None

    def running(self):
        return self.live

    def start(self):
        if self.live:
            return False
        self.log.append("start")
        self.live = True
        return True

    def mark_verified(self, incarnation):
        if incarnation is not None and incarnation is self.token and self.live:
            self.verified = True

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


def test_a_running_engine_survives_a_failed_probe() -> None:
    main, runtime, ModelLoadError = boot()
    log: list[str] = []
    engine = FakeEngine(log, live=True)
    runtime._manager = engine
    main.client = FakeClient(ModelLoadError("momentary refusal"), log)
    try:
        run(main._ensure_bundled_engine())
        check("a verification refusal still fails the call", False)
    except ModelLoadError:
        check("a verification refusal still fails the call", True)
    check("but does not stop an engine this call did not launch", "stop" not in log and engine.live, str(log))
    log = []
    hung = FakeEngine(log, live=True, ready=False)
    runtime._manager = hung
    main.client = FakeClient(None, log)
    try:
        run(main._ensure_bundled_engine())
    except ModelLoadError:
        pass
    check("an engine that gives no HTTP answer for the whole window is hung and is stopped, launched here or not",
          log[-1] == "stop" and not hung.live, str(log))
    check("a refusal after another caller launched it leaves it running", engine.live)


def test_verified_flag_gates_plain_http_users() -> None:
    main, runtime, ModelLoadError = boot()
    from service import config
    import urllib.request
    calls: list[str] = []
    original = urllib.request.urlopen

    def spy(*args, **kwargs):
        calls.append("urlopen")
        raise AssertionError("the engine key must not be sent")

    urllib.request.urlopen = spy
    original_open = urllib.request.OpenerDirector.open
    urllib.request.OpenerDirector.open = lambda self, *a, **k: spy()
    try:
        log: list[str] = []
        engine = FakeEngine(log, live=False)
        runtime._manager = engine
        check("no engine: plain-HTTP push is skipped", config._push_context_window_to_server("m", 4096) is False)
        engine.live = True
        check("a live but unverified engine gets no key either", config._push_context_window_to_server("m", 4096) is False)
        check("the client never opened a connection", not calls, str(calls))
        main.client = FakeClient(None, log)
        run(main._ensure_bundled_engine())
        check("verification marks the engine verified", engine.verified is True)
        stale = FakeEngine(log, live=True)
        before = stale.incarnation()
        stale.token = object()                       # the child was replaced during the check
        stale.mark_verified(before)
        check("a stale verification cannot mark a replaced child verified", stale.verified is False)
        from service.config.endpoints import Endpoint, EndpointConfigurationError
        try:
            Endpoint("local", "http://127.0.0.1:8000", "local_omlx", managed=True).api_key()
            check("the engine key is refused for another port", False)
        except EndpointConfigurationError:
            check("the engine key is refused for another port", True)
    finally:
        urllib.request.urlopen = original
        urllib.request.OpenerDirector.open = original_open


def test_reap_orphan_is_safe_without_a_pid_file() -> None:
    main, runtime, ModelLoadError = boot()
    runtime._manager = None
    check("no engine.pid: nothing to reap, in any mode", runtime.reap_orphan() is False)


def main() -> int:
    for fn in (test_leftover_engine_is_not_probed_first, test_live_engine_is_verified_not_restarted,
               test_readiness_then_identity, test_a_running_engine_survives_a_failed_probe,
               test_verified_flag_gates_plain_http_users, test_reap_orphan_is_safe_without_a_pid_file):
        print(fn.__name__)
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
