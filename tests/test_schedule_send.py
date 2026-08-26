"""schedule_send `text`/`body` alias — regression test.

Reported live (Wisp debug export, 2026-08-23): "schedule send a message to
mom at 9pm with my schedule for tmrow" first called schedule_send with a
`text` argument — send_message/draft_message's name for the message content —
instead of `body`, which is schedule_send's own name for it (matching
send_email). The call was rejected: "unexpected argument(s) ['text']; missing
required argument(s) ['body']", burning a whole extra agent step before the
model retried with `body` and it went through. schedule_send now accepts
`text` as an alias for `body`, so a channel='message' call reaching for
send_message's parameter name still succeeds on the first try.

    .venv/bin/python tests/test_schedule_send.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service.assistant.outbound_queue as outbound_queue_mod  # noqa: E402
from service.tools import action_tools  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def _patched_queue():
    """Swap the real (SQLite-backed) outbound_queue.add for an in-memory
    fake, so these tests never write a row into the user's real scheduled
    sends. Returns (calls, restore)."""
    calls: list[dict] = []

    def fake_add(*, channel, recipient, body, when_ts, subject="", display=""):
        calls.append({"channel": channel, "recipient": recipient, "body": body,
                      "subject": subject, "display": display})
        return "fakeid0001"

    orig_add = outbound_queue_mod.outbound_queue.add
    outbound_queue_mod.outbound_queue.add = fake_add

    def restore():
        outbound_queue_mod.outbound_queue.add = orig_add

    return calls, restore


def test_text_alias_reaches_the_queue_as_body() -> None:
    print("\nschedule_send: `text` is accepted as an alias for `body`")
    calls, restore = _patched_queue()
    try:
        result = asyncio.run(action_tools.schedule_send(
            channel="message", to="1 (650) 796-1110", when="in 10 minutes",
            text="Here's your schedule for tomorrow."))
    finally:
        restore()

    check("call succeeded, not an error/refusal",
          result.startswith("Scheduled:"), result)
    check("the queue actually received the text as `body`",
          calls and calls[0]["body"] == "Here's your schedule for tomorrow.", calls)


def test_body_still_works_as_before() -> None:
    print("\nschedule_send: `body` (the documented name) still works")
    calls, restore = _patched_queue()
    try:
        result = asyncio.run(action_tools.schedule_send(
            channel="email", to="dan@example.com", when="in 10 minutes",
            body="See you then.", subject="Re: plans"))
    finally:
        restore()

    check("call succeeded", result.startswith("Scheduled:"), result)
    check("body reached the queue unchanged",
          calls and calls[0]["body"] == "See you then.", calls)


def test_neither_body_nor_text_is_rejected() -> None:
    print("\nschedule_send: refuses when neither `body` nor `text` is given")
    result = asyncio.run(action_tools.schedule_send(
        channel="message", to="1 (650) 796-1110", when="in 10 minutes"))
    check("refused with a clear message",
          "pass `body`" in result, result)


def main() -> int:
    test_text_alias_reaches_the_queue_as_body()
    test_body_still_works_as_before()
    test_neither_body_nor_text_is_rejected()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
