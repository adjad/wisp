"""The backend the app launched ends itself once Wisp is gone.

Wisp never adopts, reclaims or signals a program it finds on its port at launch,
including a backend an earlier Wisp left behind (BackendOwnership.swift). So a
backend that outlives the app (a crash, a Force Quit) would hold the port, the
model and the schedulers with no window, and every later launch would stop at
"Wisp can't start its service" until someone killed it by hand. The app keeps
that rule; this module closes the gap from the other side.

Only in the backend the app launched (it passes WISP_LAUNCH_NONCE), and only
when its parent is Wisp itself: a script, a test or a wrapper that starts the
service is never watched. Once the parent is gone the process sends itself
SIGTERM, which is what the app sends on quit: uvicorn shuts down and the
lifespan stops the bundled engine. A shutdown that hangs is cut after a bound:
the engine is stopped here and the process exits.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Mapping

LAUNCH_NONCE_ENV = "WISP_LAUNCH_NONCE"
# The app's executable: Wisp in the packaged app, WispApp from a developer build
# (the same names service/wisp_reset.caller_is_parent_app accepts).
APP_EXECUTABLES = frozenset({"Wisp", "WispApp"})
POLL_SECONDS = 1.0
# How long the normal shutdown gets after SIGTERM before the hard exit. Longer
# than the app's own wait on quit (5 s), which nobody is left to enforce.
EXIT_BOUND_SECONDS = 8.0
ENGINE_GRACE_SECONDS = 2.0

OFF, WATCH, GONE = "off", "watch", "gone"

_lock = threading.Lock()
_thread: threading.Thread | None = None


def _parent_executable(pid: int) -> str:
    from service.inference.local_peer import process_path
    return Path(process_path(pid)).name


def _stop_engine() -> None:
    """What the lifespan does on a normal shutdown: stop the bundled engine for
    good. Never a user's own oMLX."""
    from service.engine.mode import is_bundled
    if is_bundled():
        from service.engine.runtime import stop_if_running
        stop_if_running(final=True, grace=ENGINE_GRACE_SECONDS)


def decide(environ: Mapping[str, str], parent: int,
           executable: Callable[[int], str] = _parent_executable) -> str:
    """OFF: not the app's backend, or the parent is not Wisp; never watched.
    GONE: the app launched this process and has already gone (it can die while
    the service is still importing). WATCH: the parent is Wisp and alive."""
    if not environ.get(LAUNCH_NONCE_ENV):
        return OFF
    if parent <= 1:
        return GONE
    try:
        name = executable(parent)
    except Exception:  # noqa: BLE001 — a parent that can't be read is never watched
        return OFF
    return WATCH if name in APP_EXECUTABLES else OFF


def end_process(*, kill: Callable[[int, int], None] = os.kill, pid: int | None = None,
                sleep: Callable[[float], None] = time.sleep,
                stop_engine: Callable[[], None] = _stop_engine,
                exit: Callable[[int], None] = os._exit,
                bound: float = EXIT_BOUND_SECONDS) -> None:
    """SIGTERM to this process, once. If it is still here after `bound`, stop the
    engine and exit without waiting for anything else."""
    print("[parent-watch] Wisp is no longer running; stopping its service.", file=sys.stderr, flush=True)
    kill(os.getpid() if pid is None else pid, signal.SIGTERM)
    sleep(bound)
    try:
        stop_engine()
    except Exception:  # noqa: BLE001 — the exit below must still happen
        pass
    exit(0)


def watch(parent: int, *, getppid: Callable[[], int] = os.getppid,
          sleep: Callable[[float], None] = time.sleep, poll: float = POLL_SECONDS,
          end: Callable[[], None] = end_process) -> None:
    """Returns only after `end` ran. A process whose parent exits is handed to
    launchd, so its parent pid changes; the recorded pid being reused by another
    program cannot hide that."""
    while getppid() == parent:
        sleep(poll)
    end()


def start(*, environ: Mapping[str, str] | None = None, getppid: Callable[[], int] = os.getppid,
          executable: Callable[[int], str] = _parent_executable,
          end: Callable[[], None] = end_process, sleep: Callable[[float], None] = time.sleep,
          poll: float = POLL_SECONDS) -> str:
    """Start the watch when this is the app's backend. Returns what was decided
    (OFF, WATCH or GONE). At most one watch per process."""
    global _thread
    parent = getppid()
    mode = decide(os.environ if environ is None else environ, parent, executable)
    if mode == OFF:
        return OFF
    with _lock:
        if _thread is not None:
            return mode
        def run() -> None:
            if mode == GONE:
                end()
            else:
                watch(parent, getppid=getppid, sleep=sleep, poll=poll, end=end)

        _thread = threading.Thread(target=run, name="wisp-parent-watch", daemon=True)
        _thread.start()
    return mode
