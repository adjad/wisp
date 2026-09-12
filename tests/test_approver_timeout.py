"""InteractiveApprover.confirm() timeout — regression tests for docs/OPTIMIZATION_BACKLOG.md #5.

The bug: confirm() awaited its future with no timeout, so an unanswered card
hung the turn forever — and because idle.begin_foreground()/end_foreground()
brackets the whole /agent request, a stuck confirm also silently disabled the
daily brief and profile rotation for as long as the card sat unanswered.

What must keep holding:
  * an unanswered action resolves to DENIED (fail safe), not allowed, once the
    timeout elapses — never left hanging;
  * a `confirm_timeout` event fires so the client can clear a stale card,
    distinct from a real user "deny";
  * a real answer that arrives before the timeout still wins normally;
  * resolve() called AFTER the timeout has already fired is a harmless no-op
    (the action_id is no longer pending) — it must not raise or double-resolve.

    .venv/bin/python tests/test_approver_timeout.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent.approver import InteractiveApprover  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def test_unanswered_action_times_out_denied() -> None:
    print("\nan unanswered card denies itself instead of hanging forever")
    events = []

    async def emit(ev: dict) -> None:
        events.append(ev)

    async def run() -> bool:
        approver = InteractiveApprover(emit, timeout=0.05)
        return await approver.confirm({"id": "a1", "tool": "run_shell", "args": {}})

    approved = asyncio.run(run())
    check("timed-out action is denied", approved is False)
    types = [e["type"] for e in events]
    check("a confirm event fired", "confirm" in types)
    check("a confirm_timeout event fired", "confirm_timeout" in types)
    timeout_ev = next(e for e in events if e["type"] == "confirm_timeout")
    check("confirm_timeout names the right action", timeout_ev["id"] == "a1")


def test_real_answer_before_timeout_wins() -> None:
    print("\na real answer that beats the clock is used, not overridden")
    events = []

    async def emit(ev: dict) -> None:
        events.append(ev)

    async def run() -> bool:
        approver = InteractiveApprover(emit, timeout=5.0)
        task = asyncio.create_task(approver.confirm({"id": "a2", "tool": "write_file", "args": {}}))
        await asyncio.sleep(0)  # let confirm() emit and start waiting
        resolved = approver.resolve("a2", True, "once")
        check("resolve() found the pending action", resolved)
        return await task

    approved = asyncio.run(run())
    check("the real answer (approve) is honored", approved is True)
    check("no confirm_timeout fired", all(e["type"] != "confirm_timeout" for e in events))


def test_late_resolve_after_timeout_is_a_harmless_noop() -> None:
    print("\na resolve() that arrives after the timeout does nothing, doesn't raise")

    async def emit(ev: dict) -> None:
        pass

    async def run() -> InteractiveApprover:
        approver = InteractiveApprover(emit, timeout=0.05)
        await approver.confirm({"id": "a3", "tool": "run_shell", "args": {}})
        return approver

    approver = asyncio.run(run())
    late = approver.resolve("a3", True, "once")
    check("late resolve() reports it found nothing pending", late is False)


if __name__ == "__main__":
    test_unanswered_action_times_out_denied()
    test_real_answer_before_timeout_wins()
    test_late_resolve_after_timeout_is_a_harmless_noop()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
