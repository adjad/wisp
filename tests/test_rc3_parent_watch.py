"""The backend the app launched ends itself once Wisp is gone (a01-app-lifecycle#1).

Everything the watch touches is injected: no process is signalled, nothing
sleeps, no engine is stopped and nothing exits. The last tests read the shipped
sources for the two call sites.
"""
from __future__ import annotations

import re
import signal
import threading
from pathlib import Path

import pytest

from service import parent_watch as W

ROOT = Path(__file__).resolve().parents[1]
NONCE = {"WISP_LAUNCH_NONCE": "launch-1"}
APP = 4321


def _wisp(pid):
    return "Wisp"


class Recorder:
    """Stand-ins for kill, sleep, the engine stop and exit, in call order."""

    def __init__(self):
        self.calls = []

    def kill(self, pid, sig):
        self.calls.append(("kill", pid, sig))

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))

    def stop_engine(self):
        self.calls.append(("stop_engine",))

    def exit(self, code):
        self.calls.append(("exit", code))

    def end(self):
        W.end_process(kill=self.kill, pid=777, sleep=self.sleep, stop_engine=self.stop_engine,
                      exit=self.exit, bound=8.0)


@pytest.fixture(autouse=True)
def _one_watch_per_test(monkeypatch):
    monkeypatch.setattr(W, "_thread", None)


# -- when it arms ---------------------------------------------------------------

@pytest.mark.parametrize("environ", [{}, {"WISP_LAUNCH_NONCE": ""}])
def test_a_service_the_app_did_not_launch_is_never_watched(environ):
    assert W.decide(environ, APP, _wisp) == W.OFF
    assert W.decide(environ, 1, _wisp) == W.OFF


@pytest.mark.parametrize("name", ["python3.13", "zsh", "pytest", "Wisp Helper", "wisp", ""])
def test_a_parent_that_is_not_wisp_is_never_watched(name):
    assert W.decide(NONCE, APP, lambda pid: name) == W.OFF


def test_a_parent_that_cannot_be_read_is_never_watched():
    def unreadable(pid):
        raise RuntimeError("native_inspection_unavailable")
    assert W.decide(NONCE, APP, unreadable) == W.OFF


@pytest.mark.parametrize("name", ["Wisp", "WispApp"])
def test_the_app_as_parent_is_watched(name):
    seen = []
    assert W.decide(NONCE, APP, lambda pid: seen.append(pid) or name) == W.WATCH
    assert seen == [APP]


@pytest.mark.parametrize("parent", [1, 0])
def test_an_app_that_went_away_during_startup_counts_as_gone(parent):
    def never(pid):
        raise AssertionError("no parent is left to look at")
    assert W.decide(NONCE, parent, never) == W.GONE


# -- what it does ---------------------------------------------------------------

def test_nothing_happens_while_the_parent_is_unchanged():
    record = Recorder()
    answers = iter([APP, APP, APP, 1])
    W.watch(APP, getppid=lambda: next(answers), sleep=record.sleep, poll=1.0,
            end=lambda: record.calls.append(("end",)))
    # Three polls with the app alive: three sleeps and nothing else. Then the end, once.
    assert record.calls == [("sleep", 1.0)] * 3 + [("end",)]


def test_a_lost_parent_gets_one_sigterm_then_the_bounded_hard_exit():
    record = Recorder()
    answers = iter([APP, 1])
    W.watch(APP, getppid=lambda: next(answers), sleep=record.sleep, poll=1.0, end=record.end)
    assert record.calls == [
        ("sleep", 1.0),
        ("kill", 777, signal.SIGTERM),      # the normal shutdown, as on quit
        ("sleep", 8.0),                     # the bound it gets
        ("stop_engine",),                   # still here: the engine must not outlive it
        ("exit", 0),
    ]
    assert [call for call in record.calls if call[0] == "kill"] == [("kill", 777, signal.SIGTERM)]


def test_the_hard_exit_happens_even_if_the_engine_stop_fails():
    record = Recorder()

    def broken():
        record.calls.append(("stop_engine",))
        raise OSError("engine state unreadable")

    W.end_process(kill=record.kill, pid=777, sleep=record.sleep, stop_engine=broken, exit=record.exit, bound=8.0)
    assert record.calls[-2:] == [("stop_engine",), ("exit", 0)]


def test_the_engine_stop_is_the_quit_one_and_only_for_the_bundled_engine(monkeypatch):
    from service.engine import mode, runtime
    calls = []
    monkeypatch.setattr(runtime, "stop_if_running", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(mode, "is_bundled", lambda: False)
    W._stop_engine()
    assert calls == []                      # a user's own oMLX is never touched
    monkeypatch.setattr(mode, "is_bundled", lambda: True)
    W._stop_engine()
    assert calls == [{"final": True, "grace": 2.0}]


# -- start ----------------------------------------------------------------------

def _joined():
    thread = W._thread
    assert thread is not None and thread.daemon and thread.name == "wisp-parent-watch"
    thread.join(5)
    assert not thread.is_alive()


def test_start_does_nothing_without_the_launch_nonce():
    ended = []
    assert W.start(environ={}, getppid=lambda: APP, executable=_wisp, end=lambda: ended.append(1)) == W.OFF
    assert W._thread is None and ended == []


def test_start_does_nothing_under_a_wrapper_or_a_test_runner():
    ended = []
    assert W.start(environ=NONCE, getppid=lambda: APP, executable=lambda pid: "python3.13",
                   end=lambda: ended.append(1)) == W.OFF
    assert W._thread is None and ended == []


def test_start_watches_the_app_and_ends_once_it_is_gone():
    ended, release = [], threading.Event()
    answers = iter([APP, APP, APP, 1])      # start's own read, then the polls

    def sleep(seconds):
        assert release.wait(5)

    assert W.start(environ=NONCE, getppid=lambda: next(answers), executable=_wisp,
                   end=lambda: ended.append("end"), sleep=sleep, poll=1.0) == W.WATCH
    assert ended == []                      # the app is alive: nothing yet
    release.set()
    _joined()
    assert ended == ["end"]


def test_start_ends_at_once_when_the_app_is_already_gone():
    ended = []
    assert W.start(environ=NONCE, getppid=lambda: 1, executable=_wisp, end=lambda: ended.append("end")) == W.GONE
    _joined()
    assert ended == ["end"]


def test_a_second_start_adds_no_second_watch():
    ended, release = [], threading.Event()

    def sleep(seconds):
        assert release.wait(5)

    def start():
        return W.start(environ=NONCE, getppid=lambda: APP if not release.is_set() else 1, executable=_wisp,
                       end=lambda: ended.append("end"), sleep=sleep, poll=1.0)

    assert start() == W.WATCH
    first = W._thread
    assert start() == W.WATCH and W._thread is first
    release.set()
    _joined()
    assert ended == ["end"]


def test_the_defaults_are_the_real_process_and_sane_bounds():
    assert W.LAUNCH_NONCE_ENV == "WISP_LAUNCH_NONCE"
    assert W.APP_EXECUTABLES == {"Wisp", "WispApp"}
    assert 0 < W.POLL_SECONDS <= 2
    # Longer than the app's own 5 s wait on quit, and still short.
    assert 5 < W.EXIT_BOUND_SECONDS <= 15


# -- the two call sites, read from the sources ------------------------------------

def _body(text: str, signature: str) -> str:
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[open_at:i + 1]
    raise AssertionError(f"unbalanced body after {signature!r}")


def test_lifespan_starts_the_watch_only_for_the_launched_backend_with_a_real_app():
    import inspect
    from service import main
    source = inspect.getsource(main.lifespan)
    guard = re.search(
        r'\n    if app is not None and os\.environ\.get\("WISP_LAUNCH_NONCE"\):\n'
        r'(?:        #[^\n]*\n)*'
        r'        from service import parent_watch\n'
        r'        parent_watch\.start\(\)\n', source)
    assert guard, "lifespan must start the watch under the nonce, and never for lifespan(None)"
    assert source.count("parent_watch.start(") == 1
    # Before the slow startup work, so an app that dies early is still noticed.
    assert guard.start() < source.index("reap_orphan")
    # Not in the module preamble (tests/test_wisp_reset.py pins its first statements).
    module = (ROOT / "service" / "main.py").read_text(encoding="utf-8")
    assert module.count("parent_watch") == 2


def test_quit_from_an_alert_signals_the_backend_this_app_spawned():
    app = (ROOT / "app" / "Sources" / "WispApp" / "AppDelegate.swift").read_text(encoding="utf-8")
    body = "\n".join(line.split("//", 1)[0] for line in
                     _body(app, "private func terminateWithoutShutdown()").splitlines())
    assert body.lstrip("{ \n").startswith("noteTerminating()")
    assert body.index("noteTerminating()") < body.index("backend.stop()") < body.index("exitNow()")
    # Not the full quit: that would ask a port the app has just refused to stop its engine.
    for forbidden in ("shutdownOMLX", "beginShutdown", "quit()", "kill(", "stopAndWait"):
        assert forbidden not in body, forbidden
    # stop() signals only the Process object this manager spawned, never a pid found by port.
    manager = (ROOT / "app" / "Sources" / "WispApp" / "BackendManager.swift").read_text(encoding="utf-8")
    stop = _body(manager, "func stop()")
    assert "detachForStop()" in stop and "process.terminate()" in stop
    assert "kill(" not in stop and "lsof" not in stop
