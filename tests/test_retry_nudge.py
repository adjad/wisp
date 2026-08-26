"""The forced-tool retry ladder must actually CHANGE the request — regression tests.

When a step is forcing or expecting a tool call and the model replies in plain
text instead, `run_agent` appends a correcting "you must call the tool now"
nudge and retries, up to 3 attempts. The whole ladder is worthless if the retry
sends the same bytes as the first try.

VERIFIED BROKEN 2026-08-09, which is why this file exists. `_fit_window` returns
a COPY of the message list (`msgs = list(msgs)`), and `step_msgs` was built ONCE
above the attempt loop. The nudge was appended to `msgs` — the original — so
every attempt re-sent the identical `step_msgs`. Worse, a forced step is pinned
to temperature 0.0 (greedy), so an identical request decodes to an identical
response by construction: attempts 2 and 3 could not have differed from attempt
1 even in principle.

That exactly matches the failure already recorded in service/config/__init__.py:
"a `required` first step failed to produce a tool call on ALL THREE attempts,
burning 38s ... The third attempt even opened 'I understand you want me to use
tools, but…'". Three identical requests, three identical refusals, ~25s of the
38s spent proving determinism.

    .venv/bin/python tests/test_retry_nudge.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


class FakeClient:
    """Records every request, and always answers in plain text — i.e. always
    refuses to call the tool, so the full retry ladder runs every time."""

    def __init__(self) -> None:
        self.requests: list[list[dict]] = []
        self.temperatures: list[float | None] = []

    async def ensure_only(self, model, *, exclusive=False, emit=None, **kw):
        return None

    async def stream_events(self, model, messages, *, tools=None, tool_choice=None,
                            temperature=None, max_tokens=2048, **extra):
        # Deep-ish copy: the loop mutates its own list, and we need the state
        # AS SENT, not as it ends up.
        self.requests.append([dict(m) for m in messages])
        self.temperatures.append(temperature)
        yield {"kind": "content", "text": "I understand you want me to use tools, but"}
        yield {"kind": "final",
               "message": {"role": "assistant",
                           "content": "I understand you want me to use tools, but",
                           "tool_calls": None}}


class Approver:
    async def confirm(self, action):
        return False


async def emit(ev):
    return None


def run(**kw) -> FakeClient:
    c = FakeClient()
    asyncio.run(loop.run_agent(
        c, "Agents-A1-4B-oQe6", [{"role": "user", "content": "What's my battery health?"}],
        emit, Approver(), max_steps=1, **kw))
    return c


def texts(req: list[dict]) -> str:
    return "\n".join(str(m.get("content") or "") for m in req)


# --------------------------------------------------------------------------

def test_forced_tool_retry_sends_the_nudge() -> None:
    print("\nforce_first_tool: each retry must carry the correcting nudge")
    c = run(tools=["get_battery_status"], force_first_tool="get_battery_status")
    check("all 3 attempts were made", len(c.requests) == 3, f"{len(c.requests)}")
    if len(c.requests) < 3:
        return
    check("attempt 2 differs from attempt 1 (the nudge reached the model)",
          texts(c.requests[1]) != texts(c.requests[0]),
          "identical request re-sent")
    check("attempt 2 actually contains the force nudge",
          "must call the get_battery_status tool now" in texts(c.requests[1]),
          texts(c.requests[1])[-200:])
    check("attempt 3 carries BOTH nudges, not just the latest",
          texts(c.requests[2]).count("must call the get_battery_status tool now") == 2,
          f"count={texts(c.requests[2]).count('must call the get_battery_status tool now')}")
    check("attempt 3 also carries the model's own rejected replies",
          texts(c.requests[2]).count("I understand you want me to use tools") == 2,
          texts(c.requests[2])[-300:])


def test_expect_tool_first_retry_sends_the_nudge() -> None:
    print("\nexpect_tool_first: the softer 'needs some tool' nudge must land too")
    c = run(tools=["get_battery_status"], expect_tool_first=True)
    check("all 3 attempts were made", len(c.requests) == 3, f"{len(c.requests)}")
    if len(c.requests) < 3:
        return
    check("attempt 2 differs from attempt 1",
          texts(c.requests[1]) != texts(c.requests[0]), "identical request re-sent")
    check("attempt 2 contains the generic tool nudge",
          "requires an action you can only do by calling a tool" in texts(c.requests[1]),
          texts(c.requests[1])[-200:])


def test_growing_request_stays_within_the_window() -> None:
    print("\nthe nudge must be re-fitted to the context window, not appended blindly")
    # Re-fitting per attempt is the actual fix; this guards the reason it has to
    # be a re-fit rather than a plain append. A step that is already near the
    # window and then grows by two messages per attempt would otherwise walk
    # straight into the `400 Bad Request` _fit_window exists to prevent.
    c = run(tools=["get_battery_status"], force_first_tool="get_battery_status")
    from service.config import model_context_window
    window = model_context_window("Agents-A1-4B-oQe6")
    for i, req in enumerate(c.requests):
        est = loop._est_tokens(req) + loop._MIN_OUTPUT_TOKENS
        check(f"attempt {i + 1} still fits the {window}-token window",
              est <= window, f"~{est} tokens")


def test_ordinary_step_is_not_retried() -> None:
    print("\nan ordinary (unforced) step still answers in ONE attempt")
    # The ladder must not become a general 'always try 3 times' — that would
    # triple the cost of every normal turn.
    c = run(tools=["get_battery_status"])
    check("exactly one request for a plain text answer", len(c.requests) == 1,
          f"{len(c.requests)}")


if __name__ == "__main__":
    test_forced_tool_retry_sends_the_nudge()
    test_expect_tool_first_retry_sends_the_nudge()
    test_growing_request_stays_within_the_window()
    test_ordinary_step_is_not_retried()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
