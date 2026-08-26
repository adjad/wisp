"""A sync tool must not run on the event loop — regression tests.

18 registered tools are plain `def`s, and their blocking budgets are large:
run_shell's subprocess.run has timeout=120, run_speed_test 45, every
_osascript/_run helper 20, and read_file's PDF/docx/xlsx extraction is pure CPU
over as many as 200 pages. `run_tool` called them inline, so for the whole of
that time uvicorn's single event loop made no progress — no SSE heartbeats (the
UI reads as hung), no /assistant/sync cache refresh, no second request.

This is THREAD concurrency, not model concurrency. It is deliberately NOT the
parallel-tool-execution work that was built, measured (27.3s serial vs 37.3s
concurrent) and reverted — see the block comment in agent/loop.py. Exactly one
tool still runs at a time, and nothing here touches oMLX.

    .venv/bin/python tests/test_tool_dispatch.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.tools.registry import REGISTRY, Tool, run_tool  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def _tool(name: str, func, category: str = "fs_read") -> Tool:
    t = Tool(name=name, description=f"fake {name}",
             parameters={"type": "object", "properties": {}},
             category=category, func=func)
    REGISTRY[name] = t
    return t


# --------------------------------------------------------------------------

def test_sync_tool_does_not_block_the_loop() -> None:
    print("\na blocking sync tool lets the event loop keep running")
    # The real symptom this reproduces: heartbeats stop arriving mid-tool, so a
    # working turn looks hung. Ticks stand in for heartbeats.
    t = _tool("fake_blocking", lambda: (time.sleep(0.6), "done")[1])
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    async def main():
        hb = asyncio.create_task(heartbeat())
        res = await run_tool(t, {})
        hb.cancel()
        return res

    res = asyncio.run(main())
    check("the tool still returns its result", res == "done", f"-> {res!r}")
    check(f"the loop ticked while it blocked (got {ticks})", ticks >= 5,
          "loop was frozen for the tool's whole duration")


def test_sync_tool_runs_off_the_main_thread() -> None:
    print("\nthe sync call actually happens on a worker thread")
    main_thread = threading.current_thread().name
    t = _tool("fake_thread", lambda: threading.current_thread().name)
    where = asyncio.run(run_tool(t, {}))
    check("ran on a different thread than the loop", where != main_thread,
          f"ran on {where!r}, loop is {main_thread!r}")


def test_async_tool_still_awaited_directly() -> None:
    print("\nan async tool is still awaited, not thrown at a thread")
    async def coro():
        await asyncio.sleep(0)
        return "async-result"
    t = _tool("fake_async", coro)
    check("async tool returns normally", asyncio.run(run_tool(t, {})) == "async-result")


def test_sync_tool_returning_an_awaitable() -> None:
    print("\na sync def that RETURNS a coroutine is still resolved")
    # to_thread only resolves the CALL; a plain def handing back a coroutine
    # would otherwise stringify as "<coroutine object …>".
    async def inner():
        return "inner-result"
    t = _tool("fake_returns_coro", lambda: inner())
    check("the returned awaitable is awaited",
          asyncio.run(run_tool(t, {})) == "inner-result")


def test_errors_still_become_tool_results() -> None:
    print("\nthe error contract is unchanged across the thread boundary")
    # run_tool's whole design is that a bad call becomes a readable tool_result
    # the model can correct from, never an exception that kills the turn.
    def boom():
        raise RuntimeError("kaboom")
    t = _tool("fake_boom", boom)
    out = asyncio.run(run_tool(t, {}))
    check("a raising tool returns an error string, not a traceback",
          out.startswith("(error running fake_boom") and "kaboom" in out, f"-> {out!r}")

    def needs_arg(required):
        return required
    t2 = _tool("fake_typeerror", needs_arg)
    out2 = asyncio.run(run_tool(t2, {"wrong": 1}))
    check("a TypeError still gets the corrective 'call it again' message",
          "Call it again with corrected arguments" in out2, f"-> {out2!r}")


def test_two_tools_do_not_overlap() -> None:
    print("\nstill strictly one tool at a time (this is not parallel execution)")
    live = 0
    peak = 0

    def busy():
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        time.sleep(0.2)
        live -= 1
        return "ok"

    t = _tool("fake_busy", busy)

    async def main():
        # Sequential awaits, the shape run_agent uses for a multi-call step.
        for _ in range(3):
            await run_tool(t, {})

    asyncio.run(main())
    check(f"never more than one concurrent execution (peak {peak})", peak == 1)


if __name__ == "__main__":
    test_sync_tool_does_not_block_the_loop()
    test_sync_tool_runs_off_the_main_thread()
    test_async_tool_still_awaited_directly()
    test_sync_tool_returning_an_awaitable()
    test_errors_still_become_tool_results()
    test_two_tools_do_not_overlap()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
