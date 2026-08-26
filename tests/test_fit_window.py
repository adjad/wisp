"""_fit_window's order of sacrifice — regression tests.

This is the function that decides what to give up when a request doesn't fit the
model's context window, and getting its ORDER wrong is what produced thin,
truncated answers.

The failure, from a real debug export (2026-08-08 20:29): an ambiguous prompt
took the unscoped route — 57 tool schemas (~9,600 tokens) plus a ~5,200-token
system prompt = ~14,800 of a 16,000-token window before any message. Step 1 shrank
the output reservation to 804 tokens and stopped there, because 804 was above the
then-floor of 600. The model spent all 804 on chain-of-thought, hit the ceiling,
and `_demote_unclosed_think` reclassified the whole generation as reasoning — the
step produced no answer and no tool call at all.

The fix is the FLOOR, not a re-ordering: `_MIN_OUTPUT_TOKENS` is high enough
(2000) that step 1 cannot satisfy the budget on its own when the prompt is large,
so the function falls through to step 2 (drop oldest history) and step 3 (drop
tool schemas). Output is effectively sacrificed LAST, which is what you want when
an output token costs ~137x a prompt token in wall time and ~70% of output tokens
are thinking.

What must keep holding:
  * the output reservation is never returned below _MIN_OUTPUT_TOKENS;
  * a too-large request drops HISTORY before it drops TOOLS;
  * the system prompt, the latest user turn, and the newest tool result are
    never dropped, whatever the pressure;
  * the user's question survives even once it is no longer the last message —
    the protection is IDENTITY-based, not positional. The positional version
    silently stopped protecting it the moment the agent loop appended an
    assistant tool-call and a tool result, which is every multi-step turn;
  * an assistant tool_calls payload counts toward the budget. Those messages
    carry content "" and put everything in `tool_calls`, so counting only
    `content` scored a write_file call embedding a whole file body at ZERO;
  * a forced first tool always survives, and the toolset never goes below
    _MIN_TOOLS;
  * whatever comes back actually fits the window.

    .venv/bin/python tests/test_fit_window.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service.config as config  # noqa: E402
from service.agent import loop  # noqa: E402

PASS, FAIL = 0, 0
WINDOW = 16000  # the window the reported failure happened in


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def fit(msgs, schemas, max_tokens=3000, force=None, window=WINDOW):
    """_fit_window against a pinned window, independent of oMLX's live config."""
    real = config.model_context_window
    config.model_context_window = lambda _m: window  # type: ignore[assignment]
    try:
        return loop._fit_window(msgs, schemas, max_tokens, "test-model", force)
    finally:
        config.model_context_window = real  # type: ignore[assignment]


def tool(name: str, size: int) -> dict:
    """A schema whose serialized size is roughly `size` tokens."""
    return {"type": "function",
            "function": {"name": name, "description": "x" * (size * 4),
                         "parameters": {"type": "object", "properties": {}}}}


def msg(role: str, tokens: int, tag: str = "") -> dict:
    return {"role": role, "content": (tag or "x") * 1 + "y" * (tokens * 4)}


def est(msgs, schemas) -> int:
    return loop._est_tokens(schemas) + sum(
        loop._est_tokens(m.get("content") or "") for m in msgs)


# --------------------------------------------------------------------------

def test_output_floor_is_respected() -> None:
    print("\nthe output reservation is never returned below the floor")
    # Reproduce the reported shape: ~5,200-token system prompt + ~9,600 tokens
    # of tool schemas in a 16,000 window.
    msgs = [msg("system", 5200), msg("user", 40)]
    schemas = [tool(f"t{i}", 170) for i in range(57)]
    _, _, out = fit(msgs, schemas)
    check(f"output ({out}) >= _MIN_OUTPUT_TOKENS ({loop._MIN_OUTPUT_TOKENS})",
          out >= loop._MIN_OUTPUT_TOKENS)
    check("output is not the old 804-token squeeze", out > 804, f"got {out}")


def test_history_is_dropped_before_tools() -> None:
    print("\nhistory is given up before tool schemas")
    msgs = ([msg("system", 3000)]
            + [msg("user" if i % 2 == 0 else "assistant", 900, f"h{i}")
               for i in range(8)]
            + [msg("user", 100), msg("tool", 200)])
    schemas = [tool(f"t{i}", 150) for i in range(20)]
    out_msgs, out_schemas, out_tokens = fit(msgs, schemas)
    check("some history was dropped", len(out_msgs) < len(msgs),
          f"{len(msgs)} -> {len(out_msgs)}")
    check("no tool was dropped while history remained droppable",
          len(out_schemas) == len(schemas),
          f"{len(schemas)} -> {len(out_schemas)}")
    check("output kept its full reservation", out_tokens == 3000, f"got {out_tokens}")


def test_protected_messages_survive() -> None:
    print("\nsystem prompt, latest user turn and newest tool result are never dropped")
    msgs = ([msg("system", 3000, "SYS")]
            + [msg("assistant", 1200, f"old{i}") for i in range(10)]
            + [msg("user", 100, "ASK"), msg("tool", 300, "RESULT")])
    schemas = [tool(f"t{i}", 150) for i in range(20)]
    out_msgs, _, _ = fit(msgs, schemas)
    kept = [m["content"][:6] for m in out_msgs]
    check("system prompt survived", out_msgs[0]["content"].startswith("SYS"))
    check("latest user turn survived", any(c.startswith("ASK") for c in kept))
    check("newest tool result survived", any(c.startswith("RESULT") for c in kept))


def test_forced_tool_survives_and_min_tools_holds() -> None:
    print("\nunder maximum pressure the forced tool and the tool floor hold")
    # Nothing droppable except tools: only the protected messages remain.
    msgs = [msg("system", 6000), msg("user", 100), msg("tool", 2500)]
    schemas = [tool("keep_me", 400)] + [tool(f"t{i}", 400) for i in range(30)]
    _, out_schemas, _ = fit(msgs, schemas, force="keep_me")
    names = [s["function"]["name"] for s in out_schemas]
    check("the forced first tool survived", "keep_me" in names)
    check(f"at least _MIN_TOOLS ({loop._MIN_TOOLS}) tools remain",
          len(out_schemas) >= loop._MIN_TOOLS, f"got {len(out_schemas)}")


def test_result_actually_fits() -> None:
    print("\nwhatever comes back fits the window")
    for label, window in (("16k (the reported failure)", 16000),
                          ("24k (current setting)", 24000)):
        msgs = ([msg("system", 5200)]
                + [msg("assistant", 800, f"h{i}") for i in range(12)]
                + [msg("user", 100), msg("tool", 3000)])
        schemas = [tool(f"t{i}", 170) for i in range(57)]
        out_msgs, out_schemas, out_tokens = fit(msgs, schemas, window=window)
        total = est(out_msgs, out_schemas) + out_tokens
        check(f"{label}: prompt+output ({total}) <= window ({window})",
              total <= window, f"over by {total - window}")


def test_the_users_question_survives_mid_turn() -> None:
    print("\nthe user's actual request is never dropped, wherever it has slid to")
    # The old guard was purely positional ("the last two messages"), which is
    # only the user's turn while the conversation still ENDS with it. Once the
    # agent loop appends an assistant tool-call and a tool result, the last two
    # are tool traffic and the question sits at index 1 — the first thing the
    # drop loop reaches for. The model would then be reasoning over tool output
    # with no record of what it was asked.
    question = "WHAT_THE_USER_ACTUALLY_ASKED"
    msgs = [msg("system", 500)]
    msgs += [msg("user", 700), msg("assistant", 700)] * 6      # stale history
    msgs.append({"role": "user", "content": question})          # the real request
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "get_upcoming", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "c0", "content": msg("tool", 900)["content"]})

    out, _, _ = fit(msgs, [tool("t1", 2000), tool("t2", 2000)], max_tokens=3000)
    kept = [m.get("content") for m in out]
    check("the user's question survived the trim", question in kept,
          f"{len(msgs)} msgs -> {len(out)}")
    check("stale history was dropped instead", len(out) < len(msgs),
          "nothing was dropped at all — the fixture isn't under pressure")
    check("the system prompt survived", out[0]["role"] == "system")
    check("the tool result survived", out[-1]["role"] == "tool")


def test_tool_call_payloads_are_counted() -> None:
    print("\nan assistant tool_calls payload counts toward the budget")
    # These messages carry content "" and put everything in `tool_calls`, whose
    # arguments can be enormous — a write_file call embeds the whole file body.
    # Counting only `content` scored them at ZERO, so the budgeter believed the
    # request was thousands of tokens smaller than it was and let it overflow
    # into the bare 400 this function exists to prevent.
    big = "z" * (6000 * 4)      # ~6,000 tokens of file body
    heavy = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "write_file",
                                  "arguments": json.dumps({"path": "/tmp/x", "content": big})}}]}
    check("a tool_calls message is not scored as free",
          loop._est_tokens(heavy["tool_calls"]) > 5000,
          f"{loop._est_tokens(heavy['tool_calls'])}")

    msgs = [msg("system", 500), {"role": "user", "content": "save that for me"},
            heavy,
            {"role": "tool", "tool_call_id": "c0", "content": "wrote 24000 chars"}]
    out, schemas, mt = fit(msgs, [tool("write_file", 1500)], max_tokens=3000)
    # The real invariant: whatever comes back must FIT, counting the payload.
    total = loop._est_tokens(schemas) + sum(
        loop._est_tokens(m.get("content") or "")
        + (loop._est_tokens(m["tool_calls"]) if m.get("tool_calls") else 0)
        for m in out) + mt
    check(f"prompt+output ({total}) fits the window ({WINDOW})", total <= WINDOW)


if __name__ == "__main__":
    test_output_floor_is_respected()
    test_history_is_dropped_before_tools()
    test_protected_messages_survive()
    test_forced_tool_survives_and_min_tools_holds()
    test_result_actually_fits()
    test_the_users_question_survives_mid_turn()
    test_tool_call_payloads_are_counted()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
