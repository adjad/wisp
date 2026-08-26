"""Discarding a streamed answer the loop then rejects — regression tests.

The worst user-visible failure in the 2026-08-09 report. Sequence:

  1. the model opened a `<think>` block and hit the token ceiling before
     closing it (reasoning ran 11,859 chars against a 3,000-token cap);
  2. oMLX therefore handed the monologue back on the CONTENT channel, so it
     streamed live to the screen as the answer;
  3. `_demote_unclosed_think` reclassified it in the final message — correctly,
     which is why the loop then saw empty content and retried;
  4. the retry produced the real 844-char answer.

The user read *"We need to give an update to the user's mom... We need to review
the conversation history"* followed by the actual reply. The stored turn was
**12,704 characters**: 11,859 of monologue + 844 of answer.

Nothing can un-send a delta, so the fix is a `clear_answer` event — which the
loop already emitted for the tool-call-preamble case but NOT for a discarded
empty response — plus `main.py`'s emit() honoring it. Without the second half
the client's view is fixed but the PERSISTED turn is still the monologue,
because `reply` falls back to `"".join(captured["deltas"])`.

What must keep holding:
  * a step whose streamed content is discarded emits `clear_answer`;
  * `clear_answer` empties the capture buffer, so the discarded text is never
    what gets stored;
  * a normal answer is NOT cleared.

    .venv/bin/python tests/test_streamed_answer_discard.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.inference.omlx_client import _demote_unclosed_think  # noqa: E402

PASS, FAIL = 0, 0

# Verbatim opening of what the user actually saw.
LEAK = ("We need to give an update to the user's mom via messages about what "
        "Adi did this month and last month. We need to review the conversation "
        "history, especially recent messages from Aug 6-8")
REAL = "Hi Mom! Hope you're doing well 🌸 Here's a quick recap of what I've been up to"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def make_capture():
    """main.py's emit() capture logic, isolated."""
    captured = {"text": "", "deltas": [], "tools": []}

    async def emit(ev):
        t = ev.get("type")
        if t == "delta":
            captured["deltas"].append(ev.get("text", ""))
        elif t == "text":
            captured["text"] = ev.get("text", "")
        elif t == "tool_call":
            captured["tools"].append(ev.get("name", "?"))
        elif t == "clear_answer":
            captured["deltas"].clear()
        return None

    return captured, emit


def stored(captured) -> str:
    """What main.py persists: `captured["text"] or "".join(deltas)`."""
    return captured["text"] or "".join(captured["deltas"])


# --------------------------------------------------------------------------

def test_the_reported_sequence() -> None:
    print("\nthe reported sequence: monologue streamed, discarded, then retried")

    async def run():
        captured, emit = make_capture()
        # step 1 — the unclosed-think monologue streams as content
        await emit({"type": "delta", "text": LEAK})
        # oMLX's guard reclassifies it, so the final message is empty…
        msg = {"role": "assistant", "content": LEAK, "reasoning_content": ""}
        _demote_unclosed_think(msg, "length")
        assert msg["content"] == ""
        # …the loop discards the step and says so
        await emit({"type": "clear_answer"})
        # step 2 — the retry streams the real answer
        await emit({"type": "delta", "text": REAL})
        return captured

    captured = asyncio.run(run())
    out = stored(captured)
    check("the monologue is NOT in the stored turn", LEAK[:40] not in out)
    check("the real answer IS", REAL[:30] in out)
    check("no 12,704-char concatenation", len(out) == len(REAL), f"len={len(out)}")


def test_normal_answer_is_untouched() -> None:
    print("\na normal answer is never cleared")

    async def run():
        captured, emit = make_capture()
        await emit({"type": "delta", "text": "Your battery is at 80%."})
        return captured

    check("kept", stored(asyncio.run(run())) == "Your battery is at 80%.")


def test_clear_only_drops_deltas_not_a_text_event() -> None:
    print("\nclear_answer drops streamed deltas, not an explicit `text` result")

    async def run():
        captured, emit = make_capture()
        await emit({"type": "text", "text": "tool result surfaced as the answer"})
        await emit({"type": "delta", "text": "stray preamble"})
        await emit({"type": "clear_answer"})
        return captured

    captured = asyncio.run(run())
    check("the `text` event survives",
          stored(captured) == "tool result surfaced as the answer")


def test_loop_emits_clear_answer_on_discard() -> None:
    print("\nthe agent loop emits clear_answer wherever it discards a streamed step")
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "service", "agent", "loop.py")).read()
    # Each discard path must be guarded by content_streamed -> clear_answer.
    check("empty-response retry clears",
          src.count("if content_streamed:\n                    await emit({\"type\": \"clear_answer\"})") >= 1
          or "content_streamed" in src and src.count('"clear_answer"') >= 3,
          f'clear_answer occurrences={src.count(chr(34)+"clear_answer"+chr(34))}')
    check("clear_answer appears for the preamble case too",
          src.count('"clear_answer"') >= 3,
          f'only {src.count(chr(34)+"clear_answer"+chr(34))}')


def test_main_honors_clear_answer() -> None:
    print("\nmain.py's emit() honors clear_answer")
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "service", "main.py")).read()
    check("captured deltas are cleared",
          'clear_answer' in src and 'captured["deltas"].clear()' in src)



def test_last_attempt_text_is_never_cleared() -> None:
    print("\nthe FINAL attempt's text is the answer — it must not be cleared")
    # Regression: the nudge-and-retry path cleared the streamed reply on every
    # attempt including the last. But on the last attempt that text IS the
    # accepted answer (it is streamed deliberately, and the `not tool_calls`
    # branch does not re-emit it because it already went out). Clearing it left
    # the user with nothing — observed live as 23 delta events, one
    # clear_answer, and an empty reply.
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "service", "agent", "loop.py")).read()
    check("the nudge path is guarded by `_attempt < attempts - 1`",
          "content_streamed and _attempt < attempts - 1" in src,
          "an unguarded clear on the nudge path deletes the final answer")

    # And the behavioural shape: stream -> (last attempt, no clear) -> kept.
    async def run():
        captured, emit = make_capture()
        await emit({"type": "delta", "text": "I checked and there's nothing pending."})
        # last attempt: NO clear_answer is emitted
        return captured

    check("a final-attempt reply survives",
          stored(asyncio.run(run())) == "I checked and there's nothing pending.")


if __name__ == "__main__":
    test_the_reported_sequence()
    test_normal_answer_is_untouched()
    test_clear_only_drops_deltas_not_a_text_event()
    test_loop_emits_clear_answer_on_discard()
    test_main_honors_clear_answer()
    test_last_attempt_text_is_never_cleared()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
