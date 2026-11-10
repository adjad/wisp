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


class EpochClient:
    """A checked client whose health check takes a moment and is refused, as the real one is,
    when the connections are reset while it runs."""
    managed = True

    def __init__(self, error_type, log):
        self.error_type, self.log, self.epoch = error_type, log, 0

    async def health(self):
        self.log.append("health")
        seen = self.epoch
        await asyncio.sleep(0.05)
        if self.epoch != seen:
            error = self.error_type("Wisp couldn't verify its built-in AI engine just now")
            error.reason = "connection_invalidated"
            raise error
        return {"status": "ok"}

    def invalidate_connections(self):
        self.log.append("invalidate")
        self.epoch += 1


class SlowReadyEngine(FakeEngine):
    """Each caller's readiness poll returns a little later than the one before, as two polls
    with their own phase do."""
    polls, step = 0, 0.02

    async def wait_ready(self, timeout):
        self.polls += 1
        await asyncio.sleep(self.step * self.polls)
        return await super().wait_ready(timeout)


class DownClient(FakeClient):
    """Nothing listens: every health check fails to connect."""

    async def health(self):
        self.log.append("health")
        raise ConnectionError("connection refused")


def test_overlapping_starts_share_one_launch_and_one_verification() -> None:
    main, runtime, ModelLoadError = boot()
    log: list[str] = []
    engine = SlowReadyEngine(log)
    runtime._manager = engine
    main.client = EpochClient(ModelLoadError, log)

    async def two_callers():
        return await asyncio.gather(main.ensure_omlx(), main.ensure_omlx(), return_exceptions=True)

    results = run(two_callers())
    check("both overlapping starts return", results == [None, None], str(results))
    check("the engine is launched once", log.count("start") == 1, str(log))
    check("and is never stopped by a caller", "stop" not in log and engine.live, str(log))
    check("the second caller reuses the first one's readiness wait", log.count("wait_ready") == 1, str(log))
    check("and the engine ends up verified", engine.verified is True)

    # A reset from outside the start path (a request that failed while the engine was coming
    # up) lands inside the verification: the check is repeated, not treated as a refusal.
    log.clear()
    engine = FakeEngine(log)
    runtime._manager = engine
    client = EpochClient(ModelLoadError, log)
    main.client = client

    async def reset_during_the_check():
        task = asyncio.create_task(main._ensure_bundled_engine())
        while "health" not in log:
            await asyncio.sleep(0.005)
        client.invalidate_connections()
        await task

    try:
        run(reset_during_the_check())
        survived = True
    except ModelLoadError:
        survived = False
    check("a reset during the check is checked again", survived and log.count("health") == 2, str(log))
    check("and the engine it launched keeps running, verified",
          "stop" not in log and engine.live and engine.verified is True, str(log))

    # Any other refusal still fails at once and stops the engine this call launched.
    log.clear()
    engine = FakeEngine(log)
    runtime._manager = engine
    other = ModelLoadError("not the engine")
    other.reason = "process_identity_changed"
    main.client = FakeClient(other, log)
    try:
        run(main._ensure_bundled_engine())
        check("another refusal is not retried", False)
    except ModelLoadError:
        check("another refusal is not retried", log.count("health") == 1 and log[-1] == "stop", str(log))

    # A caller that waited behind a failed start makes its own attempt.
    log.clear()
    engine = SlowReadyEngine(log, ready=False)
    engine.step = 0.4
    runtime._manager = engine
    main.client = DownClient(None, log)
    original = main._await_omlx

    async def short_probe(seconds):
        return await original(min(seconds, 0.2))

    main._await_omlx = short_probe
    try:
        results = run(two_callers())
    finally:
        main._await_omlx = original
    check("a start that fails does not strand the caller behind it",
          all(isinstance(r, ModelLoadError) for r in results) and log.count("start") == 2
          and log.count("stop") == 2 and not engine.live, str(log))


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


def test_quit_seals_the_engine_in_a_backend_that_never_used_it() -> None:
    # Quitting a backend that never launched the engine has nothing of its own to stop, but a
    # model call still in flight must not launch one that then outlives Wisp.
    from service.engine import EngineError
    main, runtime, ModelLoadError = boot()
    runtime._manager = None
    try:
        check("nothing launched: nothing reported stopped", runtime.stop_if_running(final=True, grace=0.1) is False)
        try:
            runtime.manager().start()
            refused = False
        except EngineError:
            refused = True
        check("a model call after quit cannot launch the engine", refused)
        runtime._manager = None
        check("without quitting, nothing is made or stopped",
              runtime.stop_if_running() is False and runtime._manager is None)
    finally:
        runtime._manager = None


def test_a_large_engine_log_is_set_aside_at_the_next_start() -> None:
    # engine.log is appended to on every start and nothing trimmed it: about 1 MB a day.
    import stat
    from service.engine import EngineLayout, EngineManager
    from service.engine import manager as manager_module
    rotate = manager_module._rotate_log
    check("the cap is 20 MB", manager_module.LOG_ROTATE_BYTES == 20 * 1024 * 1024)
    with tempfile.TemporaryDirectory() as t:
        log = Path(t) / "data" / "engine.log"
        older = log.with_name("engine.log.1")
        log.parent.mkdir()
        rotate(log, 10)
        check("no log yet: nothing happens", not log.exists() and not older.exists())
        log.write_text("0123456789")
        rotate(log, 10)
        check("a log at the cap stays", log.read_text() == "0123456789" and not older.exists())
        log.write_text("first run, too long")
        log.chmod(0o644)
        rotate(log, 10)
        check("a log above the cap is set aside", not log.exists() and older.read_text() == "first run, too long")
        check("and the set-aside log is private", stat.S_IMODE(older.stat().st_mode) == 0o600)
        log.write_text("second run, too long")
        rotate(log, 10)
        check("the next one replaces it: two files at most",
              older.read_text() == "second run, too long"
              and sorted(p.name for p in log.parent.iterdir()) == ["engine.log.1"])
        log.write_text("third run, too long")
        original = os.replace
        try:
            def refuse(*args, **kwargs):
                raise PermissionError("no")
            os.replace = refuse
            rotate(log, 10)
            raised = False
        except OSError:
            raised = True
        finally:
            os.replace = original
        check("a log that cannot be moved is left to grow, and nothing is raised",
              not raised and log.read_text() == "third run, too long")
        link = Path(t) / "data" / "link.log"
        link.symlink_to(log)
        rotate(link, 10)
        check("a link is never moved", link.is_symlink() and log.exists())
        link.unlink()

        # At a start: the old log is set aside before the new engine's output is appended.
        class FakeChild:
            pid = os.getpid()

            def __init__(self, *args, stdout=None, **kwargs):
                stdout.write(b"new run\n")
                stdout.flush()

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        layout = EngineLayout(root=Path(t) / "engine", data_dir=Path(t) / "data")
        engine = EngineManager(layout, port=40000 + os.getpid() % 20000, api_key="a" * 64,
                               launcher=["fake-engine"])
        cap, manager_module.LOG_ROTATE_BYTES = manager_module.LOG_ROTATE_BYTES, 10
        launch, manager_module.subprocess.Popen = manager_module.subprocess.Popen, FakeChild
        try:
            engine.start()
            check("a start sets a large log aside", older.read_text() == "third run, too long")
            check("and the engine writes to a fresh private one",
                  log.read_text() == "new run\n" and stat.S_IMODE(log.stat().st_mode) == 0o600, log.read_text())
        finally:
            manager_module.LOG_ROTATE_BYTES = cap
            manager_module.subprocess.Popen = launch
            engine.stop(grace=0.1)


def main() -> int:
    for fn in (test_leftover_engine_is_not_probed_first, test_live_engine_is_verified_not_restarted,
               test_readiness_then_identity, test_a_running_engine_survives_a_failed_probe,
               test_overlapping_starts_share_one_launch_and_one_verification,
               test_verified_flag_gates_plain_http_users, test_reap_orphan_is_safe_without_a_pid_file,
               test_quit_seals_the_engine_in_a_backend_that_never_used_it,
               test_a_large_engine_log_is_set_aside_at_the_next_start):
        print(fn.__name__)
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
