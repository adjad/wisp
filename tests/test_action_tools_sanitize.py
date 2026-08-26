"""send_email/send_message draft sanitization — regression tests.

Reported live (Wisp debug exports, 2026-08-06): the on-device agent model
(LFM2.5-2.6B) sometimes hand-writes tool-call JSON that over-escapes —
backslash-n instead of a real newline, backslash-quote instead of a bare
quote — which survives json.loads as literal characters and round-trips
verbatim through AppleScript into the delivered mail/message. Separately, a
send_email call landed with a body cut off mid-sentence ("'Here is your
schedule for tomorrow (Friday", nothing else), which the confirmation card's
one-line summary didn't surface before it went out.

What must keep holding:
  * literal "\\n"/"\\t" in a drafted body become real newlines/tabs;
  * an over-escaped apostrophe ("Tomorrow\\'s") becomes a plain apostrophe;
  * a draft that opens with an unclosed quote (the truncation signature) is
    refused rather than sent;
  * ordinary short, punctuation-free texts ("omw") are NOT refused — the
    truncation check must stay narrow to that one artifact.

    .venv/bin/python tests/test_action_tools_sanitize.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def test_degarble_literal_escapes() -> None:
    print("\n_degarble: literal backslash-escapes -> real characters")
    check("backslash-n becomes a real newline",
          action_tools._degarble("line one\\nline two") == "line one\nline two")
    check("backslash-apostrophe becomes a plain apostrophe",
          action_tools._degarble("'Tomorrow\\'s Schedule - August 7")
          == "'Tomorrow's Schedule - August 7")
    check("plain text with no backslashes is untouched",
          action_tools._degarble("Sounds good, see you at 5.")
          == "Sounds good, see you at 5.")
    check("empty string is untouched", action_tools._degarble("") == "")


def test_looks_truncated() -> None:
    print("\n_looks_truncated: unclosed leading quote is the only trigger")
    check("the exact observed cutoff is caught",
          action_tools._looks_truncated(
              "'Here is your schedule for tomorrow (Friday"))
    check("a properly closed quoted phrase is not flagged",
          not action_tools._looks_truncated("'quoted phrase' - done."))
    check("a short punctuation-free text is not flagged",
          not action_tools._looks_truncated("omw"))
    check("a normal sentence without terminal punctuation is not flagged",
          not action_tools._looks_truncated("see you at the usual spot"))
    check("empty text is not flagged", not action_tools._looks_truncated(""))


def test_send_message_blocks_truncated_draft() -> None:
    print("\nsend_message: refuses a draft cut off mid-sentence")
    result = asyncio.run(action_tools.send_message(
        "+17073171671", "'Here is your schedule for tomorrow (Friday"))
    check("send was refused, not attempted", "NOT sent" in result, result)


def test_send_message_degarbles_before_sending() -> None:
    print("\nsend_message: fixes literal escapes before handing off to the app")
    calls: list[dict] = []

    async def fake_request(event_type, payload, **kw):
        calls.append(payload)
        return {"ok": True}

    orig = action_tools.app_request
    action_tools.app_request = fake_request
    try:
        result = asyncio.run(action_tools.send_message(
            "+17073171671", "Hey Dad, here's the plan:\\n\\n- 10am run\\n- 1pm meeting"))
    finally:
        action_tools.app_request = orig

    check("send went through", "Message sent" in result, result)
    check("payload has real newlines, not literal backslash-n",
          calls and calls[0]["text"] ==
          "Hey Dad, here's the plan:\n\n- 10am run\n- 1pm meeting",
          calls)


def main() -> int:
    test_degarble_literal_escapes()
    test_looks_truncated()
    test_send_message_blocks_truncated_draft()
    test_send_message_degarbles_before_sending()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
