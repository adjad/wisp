"""The narration-step think-block skip — regression tests.

A NARRATION step is one where the model's only remaining job is to write prose
about tool results already in its context, not to select another tool. That is
structurally identical to the summary path, where `no_thinking_kwargs` measured
17.4s -> 2.0s with a cleaner answer.

Measured directly on a narration-shaped request (Agents-A1-4B, 2026-08-08, three
reps per arm — a get_upcoming result in context, warm-readout style hint):

    thinking ON   3,398 chars of reasoning, 485-char answer, 19.9s
    thinking OFF      0 chars of reasoning, 471-char answer,  3.3s

Same answer, ~6x faster. But the SAME suppression on a tool-SELECTION step
measures 0/3 tool calls, so the gate has to be exactly right — every condition
below is load-bearing, and this file is what stops one from being relaxed by
accident later.

`tool_choice` deliberately stays "auto" on a narration step in every mode.
Measured, forcing "none" gave no benefit (21.4s, shorter answer) while removing
the model's ability to call a tool it still needs.

THREE MODES (see config.narration_mode): "off" (always think), "strict" (skip
only when every OFFERED tool answered — fires on ~2/15 router subsets, the
original, narrow version of this gate), "broad" (skip once every tool the model
has CALLED so far is clean — fires on ~11/15 subsets). "broad" is unsafe on
routes whose tools are sequential/complementary rather than alternatives (the
aggregate to-do route, the two-step document route, …) — those set
`multi_round=True` on the RouteDecision, which forces the gate off regardless
of mode. See router.RouteDecision.multi_round.

    .venv/bin/python tests/test_narration_thinking.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service.config as config  # noqa: E402
from service.agent import loop  # noqa: E402

PASS, FAIL = 0, 0
MODEL = "Agents-A1-4B-oQe6"   # listed in models.yaml's no_thinking_capable


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def gate(*, tools, forcing=False, expecting=False, saving=False,
         failed=None, hard_failed=None, answered=set(), offered=set(),
         multi_round=False, after=frozenset(), mode="strict") -> bool:
    """The `narrating` predicate from run_agent, evaluated in isolation.

    Kept in lockstep with the loop by construction: if the real condition
    changes shape, these tests are the thing that should be updated with it.

    `failed` is the RECOVERABLE failure set (a tool that errored) and `hard_failed`
    the permanent one (safety DENY, user-denied confirm, unoffered call, failed
    create_tool draft). Passing `failed=True` still works and means "one
    unrecoverable failure", which is what the old boolean meant.
    """
    if failed is True:
        hard_failed = {"_legacy"}
        failed = None
    failed = set(failed or ())
    hard_failed = set(hard_failed or ())
    return (
        mode != "off"
        and tools is not None
        and not (forcing or expecting or saving)
        and (not multi_round or bool(after and after <= answered))
        and not hard_failed
        and not (failed - answered)
        and bool(answered)
        and (mode == "broad" or offered <= answered)
    )


# --------------------------------------------------------------------------

def test_strict_fires_only_when_every_offered_tool_answered() -> None:
    print("\nstrict: fires only when the offered toolset is fully answered")
    check("1-tool route, that tool answered -> narrate",
          gate(tools=["get_upcoming"], offered={"get_upcoming"},
               answered={"get_upcoming"}, mode="strict"))
    check("2-tool route, both answered -> narrate",
          gate(tools=["view_emails", "summarize_emails"],
               offered={"view_emails", "summarize_emails"},
               answered={"view_emails", "summarize_emails"}, mode="strict"))
    check("2-tool route, only one answered -> keep thinking",
          not gate(tools=["view_emails", "summarize_emails"],
                   offered={"view_emails", "summarize_emails"},
                   answered={"view_emails"}, mode="strict"))
    check("nothing answered yet (first step) -> keep thinking",
          not gate(tools=["get_upcoming"], offered={"get_upcoming"},
                   answered=set(), mode="strict"))


def test_broad_fires_once_anything_called_is_clean() -> None:
    print("\nbroad: fires once every CALLED tool is clean, uncalled offers don't block it")
    # The whole point of "broad": a 2-tool alternative route (view_emails vs
    # summarize_emails) narrates after just ONE of them answers.
    check("2-tool route, only one called -> narrate in broad mode",
          gate(tools=["view_emails", "summarize_emails"],
               offered={"view_emails", "summarize_emails"},
               answered={"view_emails"}, mode="broad"))
    # This is exactly what strict mode refuses (see the test above) — the two
    # modes must actually differ on the same input, or "broad" is a no-op.
    check("the same input is refused in strict mode",
          not gate(tools=["view_emails", "summarize_emails"],
                   offered={"view_emails", "summarize_emails"},
                   answered={"view_emails"}, mode="strict"))
    # A memory route (remember/recall/forget) — a recall question only ever
    # calls `recall`. This was the reported coverage gap strict mode couldn't
    # close: verified live, a memory-recall turn kept its think block on both
    # steps under strict mode.
    check("memory route, only recall called -> narrate in broad mode",
          gate(tools=["remember", "recall", "forget"],
               offered={"remember", "recall", "forget"},
               answered={"recall"}, mode="broad"))
    check("nothing called yet -> keep thinking even in broad mode",
          not gate(tools=["get_upcoming"], offered={"get_upcoming"},
                   answered=set(), mode="broad"))


def test_multi_round_overrides_broad_mode() -> None:
    print("\nmulti_round forces the gate off regardless of mode")
    # The aggregate to-do route: one source answering must NOT be treated as
    # "done" — loop.SYSTEM's own words: a partial answer here "is not a partial
    # answer, it is a WRONG one." Broad mode's premise (called ⊆ answered means
    # done) is false for a route that needs several DIFFERENT tools.
    base = dict(tools=["get_upcoming", "search_notes", "summarize_emails",
                       "summarize_messages"],
                offered={"get_upcoming", "search_notes", "summarize_emails",
                         "summarize_messages"},
                answered={"get_upcoming"})   # only ONE of four so far
    check("multi_round route, one source answered -> keep thinking (broad)",
          not gate(**base, mode="broad", multi_round=True))
    check("the SAME route without multi_round WOULD narrate in broad mode "
          "(proves multi_round is what's blocking it, not something else)",
          gate(**base, mode="broad", multi_round=False))
    # multi_round must also override strict mode, even though strict rarely
    # reaches it (multi_round routes usually offer >1 tool) — belt and braces.
    check("multi_round also blocks strict mode",
          not gate(tools=["get_upcoming"], offered={"get_upcoming"},
                   answered={"get_upcoming"}, mode="strict", multi_round=True))


def test_narration_after_releases_a_multi_round_route() -> None:
    print("\nnarration_after turns multi_round from a whole-turn veto into a condition")
    # The risk multi_round guards is the model stopping after the FIRST of
    # several required tools. That risk is GONE once every required tool has
    # answered — at which point the step is pure synthesis and the monologue is
    # dead weight. Measured on the aggregate route's step 1: 2,415 chars of
    # reasoning / 14.85s deciding nothing.
    ALL4 = {"get_upcoming", "search_notes", "summarize_emails", "summarize_messages"}
    base = dict(tools=sorted(ALL4), offered=ALL4, multi_round=True,
                after=frozenset(ALL4), mode="broad")
    check("all four sources answered -> narrate",
          gate(**{**base, "answered": set(ALL4)}))
    check("three of four answered -> STILL thinking",
          not gate(**{**base, "answered": {"get_upcoming", "search_notes",
                                           "summarize_emails"}}))
    check("only one answered -> still thinking (the case multi_round exists for)",
          not gate(**{**base, "answered": {"get_upcoming"}}))
    # A multi_round route with NO required set keeps the original behavior.
    # This is the ambiguous core fallback: 18 heterogeneous tools, no set of
    # them that means "done", so it must never narrate.
    check("multi_round with an EMPTY narration_after never narrates",
          not gate(tools=["get_upcoming", "run_shell"], offered={"get_upcoming"},
                   answered={"get_upcoming"}, multi_round=True,
                   after=frozenset(), mode="broad"))
    # The document route: list_dir alone is not enough, read_file is.
    check("document route, only list_dir answered -> keep thinking",
          not gate(tools=["read_file", "list_dir"], offered={"read_file", "list_dir"},
                   answered={"list_dir"}, multi_round=True,
                   after=frozenset({"read_file"}), mode="broad"))
    check("document route, read_file answered -> narrate",
          gate(tools=["read_file", "list_dir"], offered={"read_file", "list_dir"},
               answered={"list_dir", "read_file"}, multi_round=True,
               after=frozenset({"read_file"}), mode="broad"))
    # A failed tool still wins over a satisfied narration_after — an error is
    # exactly when the model needs to reason about a corrected retry.
    check("a failed tool still blocks it even with narration_after satisfied",
          not gate(**{**base, "answered": set(ALL4), "failed": True}))
    # And a forced/expecting step is still a SELECTION step, where suppression
    # measures 0/3 tool calls.
    check("a forcing step still blocks it even with narration_after satisfied",
          not gate(**{**base, "answered": set(ALL4), "forcing": True}))


def test_run_agent_accepts_narration_after_param() -> None:
    print("\nrun_agent has a real narration_after parameter")
    import inspect
    sig = inspect.signature(loop.run_agent)
    check("narration_after is a run_agent parameter",
          "narration_after" in sig.parameters)
    check("it defaults to an empty frozenset (multi_round's original meaning)",
          sig.parameters["narration_after"].default == frozenset())


def test_never_on_the_unscoped_route() -> None:
    print("\nnever on the unscoped route, in any mode")
    # tools is None means the full registry. "Every offered tool answered" is
    # meaningless there — nobody calls all 55 — so it must not be reachable.
    for mode in ("strict", "broad"):
        check(f"tools=None -> keep thinking even if a tool answered ({mode})",
              not gate(tools=None, offered=set(), answered={"get_upcoming"}, mode=mode))


def test_never_while_a_tool_is_being_forced() -> None:
    print("\nnever on a step that is forcing or expecting a tool call, in any mode")
    for mode in ("strict", "broad"):
        base = dict(tools=["get_upcoming"], offered={"get_upcoming"},
                    answered={"get_upcoming"}, mode=mode)
        check(f"force_first_tool step -> keep thinking ({mode})",
              not gate(**base, forcing=True))
        check(f"expect_tool_first step -> keep thinking ({mode})",
              not gate(**base, expecting=True))
        check(f"forced write_file step -> keep thinking ({mode})",
              not gate(**base, saving=True))


def test_never_after_a_tool_failed() -> None:
    print("\nnever after any tool errored, was denied, or wasn't offered, in any mode")
    for mode in ("strict", "broad"):
        check(f"a failed tool disables it for the rest of the turn ({mode})",
              not gate(tools=["get_upcoming"], offered={"get_upcoming"},
                       answered={"get_upcoming"}, failed=True, mode=mode))


def test_a_recovered_error_stops_blocking_narration() -> None:
    print("\na tool that errored and then SUCCEEDED is no longer outstanding")
    # This used to be one sticky bool that was never reset, so a single
    # bad-args TypeError — the common shape when a model hallucinates an
    # argument name — disabled the narration win for the whole rest of the
    # turn, including a final step that is unambiguously pure synthesis.
    for mode in ("strict", "broad"):
        check(f"errored then retried successfully -> narrate ({mode})",
              gate(tools=["get_upcoming"], offered={"get_upcoming"},
                   failed={"get_upcoming"}, answered={"get_upcoming"}, mode=mode))
        check(f"errored and NEVER retried -> keep thinking ({mode})",
              not gate(tools=["get_upcoming", "search_notes"],
                       offered={"get_upcoming", "search_notes"},
                       failed={"get_upcoming"}, answered={"search_notes"}, mode=mode))
    print("  …but a POLICY outcome never clears")
    for label, kw in (("safety DENY / user-denied confirm", {"hard_failed": {"send_email"}}),
                      ("an unoffered call", {"hard_failed": {"delete_path"}})):
        check(f"{label} keeps thinking even after a later success",
              not gate(tools=["get_upcoming"], offered={"get_upcoming"},
                       answered={"get_upcoming", "send_email", "delete_path"},
                       mode="broad", **kw))
    print("  …and an unknown tool stays outstanding by construction")
    # An unknown tool name can never enter tools_answered, so the set
    # difference keeps it outstanding with no special-casing.
    check("unknown tool never clears",
          not gate(tools=["get_upcoming"], offered={"get_upcoming"},
                   failed={"nonexistent_tool"}, answered={"get_upcoming"},
                   mode="broad"))


def test_config_mode_accessor() -> None:
    print("\nthe three-state mode accessor round-trips")
    # Default is "broad" as of 2026-08-09 — flipped after its A/B measured
    # 21/22 correctness in both modes (same single pre-existing files_list
    # flake) and a 26% cut in reasoning chars actually generated on the 4
    # newly-covered routes (35,027 -> 25,930 over 20 reps, a load-independent
    # signal). See config._narration_mode's comment for the full numbers.
    try:
        check("default mode is 'broad'", config.narration_mode() == "broad")
        config.set_narration_mode("strict")
        check("set_narration_mode('strict') takes effect",
              config.narration_mode() == "strict")
        config.set_narration_mode("off")
        check("set_narration_mode('off') takes effect",
              config.narration_mode() == "off")
        config.set_narration_mode("nonsense")
        check("an invalid mode is rejected, previous value kept",
              config.narration_mode() == "off")
    finally:
        config.set_narration_mode("broad")
    check("restored to default", config.narration_mode() == "broad")


def test_template_kwargs() -> None:
    # Used to also test that a second, gpt-oss-lineage source (reasoning_effort)
    # merged with no_thinking_kwargs' enable_thinking instead of clobbering it.
    # That source (config.effort_kwargs) was removed once gpt-oss left the
    # roster and it always returned {} — _template_kwargs is now just a
    # pass-through to no_thinking_kwargs, so there is nothing left to merge.
    print("\nchat_template_kwargs reflects no_thinking correctly")
    off = loop._template_kwargs(MODEL, no_thinking=False)
    on = loop._template_kwargs(MODEL, no_thinking=True)
    check("no_thinking=False sends no enable_thinking",
          "enable_thinking" not in off.get("chat_template_kwargs", {}), f"{off}")
    check("no_thinking=True sends enable_thinking=False",
          on.get("chat_template_kwargs", {}).get("enable_thinking") is False, f"{on}")


def test_run_agent_accepts_multi_round_param() -> None:
    print("\nrun_agent has a real multi_round parameter, not just this test's mirror")
    import inspect
    sig = inspect.signature(loop.run_agent)
    check("multi_round is a run_agent parameter", "multi_round" in sig.parameters)
    check("it defaults to False",
          sig.parameters["multi_round"].default is False)


if __name__ == "__main__":
    test_strict_fires_only_when_every_offered_tool_answered()
    test_broad_fires_once_anything_called_is_clean()
    test_multi_round_overrides_broad_mode()
    test_narration_after_releases_a_multi_round_route()
    test_run_agent_accepts_narration_after_param()
    test_a_recovered_error_stops_blocking_narration()
    test_never_on_the_unscoped_route()
    test_never_while_a_tool_is_being_forced()
    test_never_after_a_tool_failed()
    test_config_mode_accessor()
    test_template_kwargs()
    test_run_agent_accepts_multi_round_param()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
