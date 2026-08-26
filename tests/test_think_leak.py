"""Unclosed-<think> leak guard — regression tests.

Reported live (Wisp debug export, 2026-08-06 20:37): "Daily summary" and
"send an email ... with a summary from today" both answered with the model's
own private planning monologue instead of an answer — "The user wants me to
create a warm, helpful inbox summary for Adi Jain. Let me analyze..." was
rendered to the user as the inbox summary.

Mechanism (reproduced against the live engine, not inferred): models with an
always-on think block (LFM2.5, which nearly every role points at) open
`<think>` before writing any answer. oMLX only routes that block to
`reasoning_content` once it sees the CLOSING tag. When generation hits the
token ceiling first, the opening tag is consumed but the monologue comes back
as `content` — the answer field — with no tag left in it, so main.py's
`<think>` regex cannot match it. Measured: summarize_emails at its old
max_tokens=800 returned finish_reason="length", reasoning_content="", and
1243 characters of monologue in `content`.

That poisoned tool result is also what broke multi-step tool sequences: the
agent loop's `last_tool_result` fallback surfaced the monologue as the turn's
final answer and returned, so a "summarize, then send it" request ended after
the summarize step and never reached send_email.

What must keep holding:
  * a truncated run with EMPTY reasoning has its content reclassified as
    reasoning, leaving content empty so callers hit their own fallbacks
    (raw inbox/message lines) instead of printing a monologue;
  * a run that stopped cleanly is never touched, however long it is;
  * a run that DID close its think block (reasoning non-empty) keeps its
    content even when the answer itself was then truncated — that is a real
    partial answer, not a leak;
  * the guard is applied to every choice of a non-streaming response.

    .venv/bin/python tests/test_think_leak.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.inference.omlx_client import _demote_unclosed_think  # noqa: E402

PASS, FAIL = 0, 0

# Verbatim opening of the leak captured in the 2026-08-06 debug export.
LEAK = ("The user wants me to create a warm, helpful inbox summary for Adi "
        "Jain. Let me analyze the emails and organize them into sections")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def test_leak_is_demoted() -> None:
    print("\nthe observed leak: truncated, no reasoning -> content reclassified")
    msg = {"role": "assistant", "content": LEAK, "reasoning_content": ""}
    _demote_unclosed_think(msg, "length")
    check("content is emptied so callers fall back", msg["content"] == "")
    check("the monologue is kept as reasoning, not discarded",
          msg["reasoning_content"] == LEAK)


def test_clean_finish_untouched() -> None:
    print("\na run that stopped cleanly is never touched")
    msg = {"content": "You have 3 new emails.", "reasoning_content": "thought"}
    _demote_unclosed_think(msg, "stop")
    check("real answer survives", msg["content"] == "You have 3 new emails.")

    # The long-but-complete case: no thinking at all (gemma's shape), clean stop.
    msg = {"content": "x" * 5000, "reasoning_content": ""}
    _demote_unclosed_think(msg, "stop")
    check("long answer with no reasoning survives a clean stop",
          len(msg["content"]) == 5000)


def test_closed_think_then_truncated_answer_survives() -> None:
    print("\nclosed think block + truncated ANSWER is a partial answer, not a leak")
    msg = {"content": "Here is your summary: you have mail from",
           "reasoning_content": "I considered the inbox"}
    _demote_unclosed_think(msg, "length")
    check("partial answer is preserved",
          msg["content"] == "Here is your summary: you have mail from")
    check("its reasoning is left alone",
          msg["reasoning_content"] == "I considered the inbox")


def test_edge_cases() -> None:
    print("\nedge cases")
    msg = {"content": "", "reasoning_content": ""}
    _demote_unclosed_think(msg, "length")
    check("empty content stays empty", msg["content"] == "")

    msg = {"content": "   \n  ", "reasoning_content": ""}
    _demote_unclosed_think(msg, "length")
    check("whitespace-only content is not promoted to reasoning",
          msg["reasoning_content"] == "")

    # reasoning present but only whitespace -> still counts as "never closed"
    msg = {"content": LEAK, "reasoning_content": "   "}
    _demote_unclosed_think(msg, "length")
    check("whitespace-only reasoning still counts as unclosed",
          msg["content"] == "" and msg["reasoning_content"] == LEAK)

    msg = {"content": LEAK, "reasoning_content": ""}
    _demote_unclosed_think(msg, None)
    check("a missing finish_reason is not treated as truncation",
          msg["content"] == LEAK)


def test_applied_to_every_choice() -> None:
    """The wiring in chat(), exercised on a response shaped like oMLX's."""
    print("\nchat() applies the guard to every choice in the response")
    data = {"choices": [
        {"finish_reason": "length",
         "message": {"content": LEAK, "reasoning_content": ""}},
        {"finish_reason": "stop",
         "message": {"content": "real answer", "reasoning_content": ""}},
    ]}
    for choice in data.get("choices") or []:
        if isinstance(choice.get("message"), dict):
            _demote_unclosed_think(choice["message"], choice.get("finish_reason"))
    check("leaked choice cleaned", data["choices"][0]["message"]["content"] == "")
    check("clean choice untouched",
          data["choices"][1]["message"]["content"] == "real answer")


def main() -> int:
    test_leak_is_demoted()
    test_clean_finish_untouched()
    test_closed_think_then_truncated_answer_survives()
    test_edge_cases()
    test_applied_to_every_choice()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
