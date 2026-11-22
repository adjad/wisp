"""ideate-loop: the runner Ideate's agent uses for waits, polling and repeated checks.

A loop runs inside this one process, so no model turn is spent on a check (the prompt budget
is tiny, config.PROMPT_BUDGET_TOKENS). The harness copies this file, standard library only,
to <project>/.ideate/bin/ideate-loop (Wisp-owned, read-only to the sandbox) and puts that
folder on the agent's PATH. It runs under the same sandbox-exec profile and folder as bash,
because it is started by bash and its children inherit the profile.

    ideate-loop --until "<cmd>" [--do "<cmd>"] [--every 2] [--max 120] [--iters 100]

Each iteration runs --do (if any), then --until; --until exiting 0 ends the loop. Every
command runs through /bin/sh -c in the project folder, in its own process group.

Caps (the values live here so the file stays standalone; config.py mirrors them and a test
keeps the two equal):
  wall time   default 120 s, hard maximum 600 s
  iterations  default 100, hard maximum 1000
  interval    default 2 s, 0.2 s to 60 s
  one check   default 30 s (never past the wall time)
  output      a check's output goes to a temp file limited to 256 KB (RLIMIT_FSIZE); only
              its last lines are kept, so memory and the summary stay small
The summary printed at the end (the only thing the model sees) is at most SUMMARY_CHARS.

SIGTERM, SIGINT and SIGHUP (Ideate's cancel, the harness aborting its tool) kill the running
check's whole group and end the loop with exit 143. Exit codes: 0 done, 1 wall-time cap,
3 iteration cap, 143 cancelled, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import os
import resource
import signal
import subprocess
import sys
import tempfile
import time

DEFAULT_MAX_S, HARD_MAX_S = 120.0, 600.0
DEFAULT_ITERS, HARD_ITERS = 100, 1000
DEFAULT_EVERY_S, MIN_EVERY_S, MAX_EVERY_S = 2.0, 0.2, 60.0
DEFAULT_CHECK_S = 30.0
OUTPUT_BYTES = 256 * 1024
TAIL_LINES, LINE_CHARS = 5, 100
SUMMARY_CHARS = 480


class _Cancel(Exception):
    pass


def _on_signal(signum, frame):  # noqa: ARG001
    raise _Cancel(signum)


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            break
        try:
            proc.wait(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        proc.wait(timeout=1.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _limit_output():
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_BYTES, OUTPUT_BYTES))
    except (ValueError, OSError):
        pass


def _run(command: str, out, limit_s: float, tracker: list) -> tuple[str, int | None]:
    """Run one shell command with output to `out`. ("ok"|"timeout", exit code or None)."""
    proc = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL, stdout=out,
                            stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=_limit_output)
    tracker.append(proc)
    end = time.monotonic() + max(0.05, limit_s)
    try:
        while True:
            code = proc.poll()
            if code is not None:
                return "ok", code
            if time.monotonic() >= end:
                _kill_group(proc)
                return "timeout", None
            time.sleep(0.05)
    finally:
        tracker.remove(proc)
        if proc.poll() is None:
            _kill_group(proc)


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace").replace("\r", "\n")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    kept = []
    for line in lines[-TAIL_LINES:]:
        kept.append(line if len(line) <= LINE_CHARS else line[:LINE_CHARS - 1] + "…")
    return "\n".join(kept)


def summary(status: str, iterations: int, elapsed: float, last_exit, tail: str, note: str = "") -> str:
    head = f"ideate-loop {status}: {iterations} checks, {elapsed:.1f}s"
    if last_exit is not None:
        head += f", last exit {last_exit}"
    if note:
        head += f" ({note})"
    text = head + ("\n" + tail if tail else "")
    if len(text) > SUMMARY_CHARS:
        text = text[:SUMMARY_CHARS - 1].rstrip() + "…"
    return text


def run_loop(until: str, do: str = "", every: float = DEFAULT_EVERY_S, max_s: float = DEFAULT_MAX_S,
             iters: int = DEFAULT_ITERS, check_s: float = DEFAULT_CHECK_S) -> tuple[int, str]:
    notes = []
    if max_s > HARD_MAX_S:
        max_s, _ = HARD_MAX_S, notes.append(f"max cut to {int(HARD_MAX_S)}s")
    if iters > HARD_ITERS:
        iters, _ = HARD_ITERS, notes.append(f"iters cut to {HARD_ITERS}")
    every = min(MAX_EVERY_S, max(MIN_EVERY_S, every))
    max_s, iters = max(1.0, max_s), max(1, iters)
    start = time.monotonic()
    deadline = start + max_s
    tracker: list = []
    fd, path = tempfile.mkstemp(prefix="ideate-loop-")
    count, last_exit, status, code = 0, None, "stopped", 1
    old = {s: signal.signal(s, _on_signal) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        with os.fdopen(fd, "wb", buffering=0) as out:
            while True:
                count += 1
                os.ftruncate(out.fileno(), 0)
                out.seek(0)
                passed = False
                for command, is_check in ((do, False), (until, True)):
                    if not command:
                        continue
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    result, exit_code = _run(command, out, min(check_s, left), tracker)
                    if is_check:
                        last_exit = exit_code if result == "ok" else "timeout"
                        passed = result == "ok" and exit_code == 0
                if passed:
                    status, code = "done", 0
                    break
                if time.monotonic() >= deadline:
                    status, code = "hit the time cap", 1
                    break
                if count >= iters:
                    status, code = "hit the iteration cap", 3
                    break
                if time.monotonic() + every >= deadline:
                    time.sleep(max(0.0, deadline - time.monotonic()))
                    status, code = "hit the time cap", 1
                    break
                time.sleep(every)
    except _Cancel:
        for proc in list(tracker):
            _kill_group(proc)
        status, code = "cancelled", 143
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    tail = _tail(path)
    try:
        os.unlink(path)
    except OSError:
        pass
    return code, summary(status, count, time.monotonic() - start, last_exit, tail, "; ".join(notes))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ideate-loop", add_help=False)
    parser.add_argument("--until", required=True)
    parser.add_argument("--do", default="")
    parser.add_argument("--every", type=float, default=DEFAULT_EVERY_S)
    parser.add_argument("--max", type=float, default=DEFAULT_MAX_S, dest="max_s")
    parser.add_argument("--iters", type=int, default=DEFAULT_ITERS)
    parser.add_argument("--check", type=float, default=DEFAULT_CHECK_S, dest="check_s")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print('usage: ideate-loop --until "<cmd>" [--do "<cmd>"] [--every 2] [--max 120] [--iters 100]')
        return 2
    code, text = run_loop(args.until, args.do, args.every, args.max_s, args.iters, args.check_s)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
