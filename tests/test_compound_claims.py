"""Compound requests must not lose a clause. Regression tests.

THE BUG CLASS. Every domain pre-check in router.py used to be an exclusive
early `return`: the first pattern to match decided the whole route, and the
rest of the sentence was silently dropped. All those patterns use .search()
(unanchored), so a rule written for a LEADING clause fires just as happily on
a trailing one and then answers for the entire request.

Reported case (2026-08-23): "create a reminder ... and tell mom about it too"
created TWO REMINDERS — one of them titled "Tell mom about canvas assignment",
which never reaches Mom, it just reminds the user to do it by hand. The model
was not confused about the intent; `send_message` was never in the offered
toolset, so the only tool it had was add_reminder. An audit then found eight
more pairs with the identical shape (see BELOW), each losing its first clause.

WHAT MUST KEEP HOLDING:
  * a compound request offers BOTH halves' tools (the nine audit rows);
  * a single-domain request routes EXACTLY as it did before claims existed —
    same tools, same force, same expect, same reason string. This is the
    overwhelmingly common path and the one a merge bug would silently widen;
  * a claim that is a more specific form of a data domain SUPPRESSES it, so a
    deliberately-narrow menu is not widened back out (a reminder is a
    calendar write: without suppression "remind me to pick up milk tonight"
    goes from 3 tools to 11, straight past the 5-7 band this project measures
    as the reliable one — 5-7 offered -> 3/3 correct, all 44 -> 2/3).

    .venv/bin/python tests/test_compound_claims.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.router.router import route  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def routed(prompt: str):
    return asyncio.run(route(prompt))


def tools_of(prompt: str) -> list[str]:
    d = routed(prompt)
    return list(d.tool_subset or []) + [n for n, _ in d.direct_calls]


# --------------------------------------------------------------------------
# The nine audit rows. Each entry: prompt, a tool the FIRST clause needs, and
# a tool the SECOND clause needs. Before the claims refactor every one of
# these offered only the second tool — the first clause was dropped outright.
_AUDIT_ROWS = [
    ("remember that I drive a BMW, and also check my calendar for today",
     "remember", "get_upcoming"),
    ("lock my screen, and remind me to take my medicine tonight",
     "lock_screen", "add_reminder"),
    ("what is queued to send, and also remind me to call the dentist tomorrow",
     "list_scheduled_sends", "add_reminder"),
    ("what's mom's number, and also add a reminder to call her tonight",
     "lookup_contact", "add_reminder"),
    ("what's new across my apps, and remind me to call the dentist tonight",
     "get_recent_activity", "add_reminder"),
    ("who do I know named Sarah, and remind me to call her tonight",
     "list_contacts", "add_reminder"),
    ("summarize my inbox, and remind me to reply to Dan tomorrow",
     "summarize_emails", "add_reminder"),
    ("what do you know about me, and check my email",
     "recall", "summarize_emails"),
    ("remind me to call the dentist tonight, and check my email",
     "add_reminder", "summarize_emails"),
]


def test_audit_rows_keep_both_halves() -> None:
    print("\nnine audit rows: BOTH clauses reach their tools")
    for prompt, first, second in _AUDIT_ROWS:
        got = tools_of(prompt)
        label = prompt[:46] + ("…" if len(prompt) > 46 else "")
        check(f"{label!r} offers {first}", first in got, f"got {got}")
        check(f"{label!r} offers {second}", second in got, f"got {got}")


def test_the_reported_case() -> None:
    print("\nthe reported case: reminder + 'tell mom about it too'")
    prompt = ("create a reminder for me to finish a canvas assignment by "
              "tonight and tell mom about it too")
    got = tools_of(prompt)
    check("the reminder is still creatable", "add_reminder" in got, f"got {got}")
    check("a real send tool is offered (not a 2nd reminder)",
          "send_message" in got or "send_email" in got, f"got {got}")
    check("the contact can be resolved", "lookup_contact" in got, f"got {got}")
    d = routed(prompt)
    check("no channel named -> asks which one", d.clarify_channel is True)
    check("marked multi-step (both halves must run)", d.multi_round is True)


def test_named_channel_skips_the_question() -> None:
    print("\nan explicitly named channel does NOT ask which one")
    d = routed("create a reminder to finish the report by tonight "
               "and text mom about it")
    got = list(d.tool_subset or [])
    check("text -> send_message offered", "send_message" in got, f"got {got}")
    check("text -> send_email NOT offered", "send_email" not in got, f"got {got}")
    check("no clarify prompt", d.clarify_channel is False)

    d = routed("create a reminder to finish the report by tonight "
               "and email dad about it")
    got = list(d.tool_subset or [])
    check("email -> send_email offered", "send_email" in got, f"got {got}")
    check("email -> send_message NOT offered", "send_message" not in got, f"got {got}")
    check("no clarify prompt", d.clarify_channel is False)


# --------------------------------------------------------------------------
# Single-domain routes pin their intended product behavior. Named day parts
# now resolve to product defaults and force the reminder write.
_SINGLE = [
    # prompt, expected reason, expected tool count, expected force
    ("remind me to pick up milk tonight",
     "reminder creation -> scoped tools (3) [time named -> forced]", 2, "add_reminder"),
    # The reminder's own CONTENT verb ("call") trips STRONG_ACTION_RE; the
    # route must still stay the narrow reminder one. See router.py's
    # "The verb belongs to the future task, not to this request."
    ("remind me to call mom", "reminder creation -> scoped tools (3)", 1, None),
    ("remember that I drive a BMW", "memory save intent -> remember", 3, "remember"),
    ("forget that I drive a BMW", "memory forget intent -> forget", 3, "forget"),
    ("what do you know about me", "self query -> recall", 1, "recall"),
    ("what's new across my apps", "recency sweep -> get_recent_activity", 5,
     "get_recent_activity"),
    ("what's queued to send", "scheduled-send queue -> scoped tools (3)", 3, None),
    # "cancel" trips the calendar-write domain and "send" trips compose; the
    # queue claim suppresses both, or this grows 3 -> 14 with a spurious
    # ask-which-channel prompt.
    ("cancel that text", "scheduled-send queue -> scoped tools (3)", 3, None),
]


def test_single_domain_routes_are_unchanged() -> None:
    print("\nsingle-domain routes keep their exact pre-refactor shape")
    for prompt, reason, n_tools, force in _SINGLE:
        d = routed(prompt)
        got_n = len(d.tool_subset or [])
        check(f"{prompt!r} reason", d.reason == reason, f"got {d.reason!r}")
        check(f"{prompt!r} offers {n_tools} tools", got_n == n_tools, f"got {got_n}")
        check(f"{prompt!r} force={force}", d.force_first_tool == force,
              f"got {d.force_first_tool!r}")


def test_device_direct_dispatch_survives() -> None:
    print("\na lone device action keeps its router-direct fast path")
    d = routed("lock my screen")
    check("still pre-dispatched", [n for n, _ in d.direct_calls] == ["lock_screen"],
          f"got {d.direct_calls}")
    # …but must NOT pre-dispatch when it is only half the request: a direct
    # call consumes step 0, which is the selection step the other half needs.
    d2 = routed("lock my screen, and remind me to take my medicine tonight")
    check("compound does NOT pre-dispatch", not d2.direct_calls, f"got {d2.direct_calls}")


def test_suppression_keeps_menus_small() -> None:
    print("\nsuppression: a reminder does not drag in the whole calendar")
    for prompt in ("remind me to pick up milk tonight",
                   "remind me to call mom",
                   "add a reminder to water the plants tonight"):
        got = tools_of(prompt)
        check(f"{prompt!r} stays narrow (<=4)", len(got) <= 4, f"got {len(got)}: {got}")
        check(f"{prompt!r} has no cancel_event", "cancel_event" not in got, f"got {got}")


def test_write_a_message_to_someone_is_a_send() -> None:
    """VERIFIED FAILURE 2026-08-23 (user's debug export): "write a message to
    my dad with my upcoming calendar events for the next month" routed as a
    read-only "messages+calendar lookup". SEND_MESSAGE_RE excludes bare
    "write" on purpose, and its target branch needs the name immediately
    after the verb ("message my dad", not "message TO my dad") — so nothing
    matched, has_write_intent was False, and neither send_message nor
    lookup_contact was ever offered. The model composed the whole message in
    chat with no way to deliver it, which reads exactly like a successful
    send."""
    print("\n'write a message to X' is a send, not a lookup")
    for prompt in (
        "write a message to my dad with my upcoming calendar events",
        "write my dad a message with my calendar",
        "compose a text to Sarah about dinner",
        "shoot my professor a message about the deadline",
    ):
        d = routed(prompt)
        got = list(d.tool_subset or [])
        check(f"{prompt[:44]!r} is read+write",
              "read+write" in d.reason or d.reason.startswith("grounded outbound report"),
              f"got {d.reason!r}")
        check(f"{prompt[:44]!r} can resolve the contact",
              "lookup_contact" in got, f"got {got}")
        check(f"{prompt[:44]!r} can actually deliver",
              bool({"send_message", "draft_message"} & set(got)), f"got {got}")


def test_composing_is_never_a_warm_readout() -> None:
    """VERIFIED FAILURE 2026-08-23 (same export): the drafted message to the
    user's DAD opened "Hey babe — here's what's coming up next month". The
    route was light_read, whose style hint says to open with a short warm
    line; the model complied and picked a term of endearment. Nothing in the
    request suggested a partner. When the model's output IS the body of a
    message to another person, that voice has to be off structurally."""
    print("\ncomposing to someone else is not a warm read-out")
    for prompt in (
        "write a message to my dad with my upcoming calendar events",
        "email mom the grocery list",
        "draft an email to dan about the report",
        "reply to dan's email",
    ):
        d = routed(prompt)
        check(f"{prompt[:44]!r} light_read is off", d.light_read is False)
    # …but a genuine read-out of the user's OWN data keeps the warm voice.
    for prompt in ("what's on my calendar this week", "summarize my messages"):
        d = routed(prompt)
        check(f"{prompt!r} stays a warm read-out", d.light_read is True)


def test_write_intent_is_per_domain() -> None:
    """One global write flag armed EVERY matched domain's write tools. On
    "remind me to call the dentist tonight, and check my email" that meant
    send_email/archive_email/trash_file on a request that only asked to LOOK
    at the inbox."""
    print("\nwrite intent belongs to the clause that carried it")
    got = tools_of("remind me to call the dentist tonight, and check my email")
    check("the reminder is creatable", "add_reminder" in got, f"got {got}")
    check("the inbox is readable", "summarize_emails" in got, f"got {got}")
    check("no send_email on a read", "send_email" not in got, f"got {got}")
    check("no trash_file on a read", "trash_file" not in got, f"got {got}")

    # …and a clause that IS an email write still gets the write tools.
    got = tools_of("summarize my inbox, and remind me to reply to Dan tomorrow")
    check("'reply to Dan' keeps reply_to_email", "reply_to_email" in got, f"got {got}")

    # A compose intent justifies the OUTBOUND tools only — never archiving,
    # flagging or trashing mail nobody mentioned.
    got = tools_of("create a reminder to finish the report by tonight "
                   "and tell mom about it too")
    check("compose keeps send_email", "send_email" in got, f"got {got}")
    check("compose does not arm trash_file", "trash_file" not in got, f"got {got}")
    check("compose does not arm archive_email", "archive_email" not in got, f"got {got}")

    # A read-then-send must not arm the READ domain's write tools: nothing is
    # being written to Notes or the calendar here.
    got = tools_of("check my notes for the wifi password and text it to me")
    check("no create_note on a notes READ", "create_note" not in got, f"got {got}")
    got = tools_of("tell my mom about my schedule for this week")
    check("no cancel_event on a calendar READ", "cancel_event" not in got, f"got {got}")


if __name__ == "__main__":
    test_audit_rows_keep_both_halves()
    test_the_reported_case()
    test_named_channel_skips_the_question()
    test_single_domain_routes_are_unchanged()
    test_device_direct_dispatch_survives()
    test_suppression_keeps_menus_small()
    test_write_a_message_to_someone_is_a_send()
    test_composing_is_never_a_warm_readout()
    test_write_intent_is_per_domain()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
