"""Outgoing-message attribution — regression tests.

Reported live (Wisp debug export, 2026-08-07): the daily brief said "Chetan
shared her Friday to-do list" and "Mom sent a warm hello" for messages the USER
had sent TO Chetan and Mom. The cached shape is `conversation | Sender: text`,
and in a one-to-one chat the conversation label IS the other person — so an
outgoing message reads `Mom | Me: Hello`, putting "Mom" where a summarizer
looks for the speaker. Asked point-blank who wrote `Chetan | Me: Hey Dad,
here's my to-do list`, the `fast` model answered "Chetan" 3/3.

The rules in identity.attribution_rules already said sender `Me` means the
user; a 2.6B model does not apply them against the pull of a leading name. The
fix is in the DATA: render_for_summary now emits `[Sender -> Recipient] text`.

Two details here are load-bearing and were each measured, not guessed:
  * SENDER FIRST. A trailing `[OUTGOING — ...]` note and an inline arrow that
    kept the `ctx |` prefix both fixed direct questions 9/9 yet still produced
    "Chetan sent you his to-do list" in a free-form summary 3/3.
  * NO COLON AFTER THE RECIPIENT. `Adi Jain (you) -> Mom: Hello` ends in the
    substring `Mom: Hello` — the very `Sender: text` pattern being disambiguated
    — and "Mom sent a friendly hello" survived in 5/6 briefs until the routing
    moved inside brackets.

Note the verbatim record (view_messages, all_message_text) deliberately keeps
the `Me` shape, so attribution_rules still has to teach both — one shape per
caller, never both at once.

    .venv/bin/python tests/test_message_attribution.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service.memory.identity as I  # noqa: E402
from service.tools.imessage_tools import render_for_summary  # noqa: E402

PASS, FAIL = 0, 0
ME = "Adi Jain"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def render(rows):
    """render_for_summary with a fixed user name (user_name() shells out)."""
    real = I._full_name
    I._full_name = ME
    try:
        return render_for_summary(rows)
    finally:
        I._full_name = real


def test_outgoing_one_to_one() -> None:
    print("\nthe reported bug: an outgoing 1:1 message is not the recipient's")
    out = render([(1_754_500_000.0, "Chetan", "Me: Hey Dad, here's my to-do list")])[0]
    check("the user leads the line, not the recipient",
          out.startswith(f"[{ME} (you) -> Chetan]"), out)
    check("the recipient never sits in a speaker slot",
          "Chetan:" not in out and "Chetan |" not in out, out)
    check("the text survives intact", out.endswith("Hey Dad, here's my to-do list"))


def test_no_name_colon_substring() -> None:
    """The 5/6-brief failure: `-> Mom: Hello` reads as `Mom: Hello`."""
    print("\nno `Name: text` substring anywhere — the colon regression")
    for txt in ("Me: Hello", "Me: Ok", "Me: Bet"):
        out = render([(1_754_500_000.0, "Mom", txt)])[0]
        body = txt.split(": ", 1)[1]
        check(f"{txt!r} leaves no 'Mom: {body}'", f"Mom: {body}" not in out, out)
    # Stated generally: no `Word: ` may follow the closing bracket.
    out = render([(1_754_500_000.0, "Mom", "Me: Hello")])[0]
    tail = out.split("] ", 1)[1]
    check("nothing after the bracket looks like a speaker label",
          not re.match(r"^[\w .'-]{1,40}:\s", tail), tail)


def test_incoming_and_groups() -> None:
    print("\nincoming, and both directions inside a group")
    out = render([(1_754_500_000.0, "Chetan", "Chetan: Ok")])[0]
    check("incoming 1:1 points AT the user", out == f"[Chetan -> {ME} (you)] Ok", out)

    rows = [(1_754_500_000.0, 'Group "Comp"', "Me: Bet"),
            (1_754_500_001.0, 'Group "Comp"', "Ethan Louie: Ready in 5")]
    a, b = render(rows)
    check("group outgoing keeps the group as recipient",
          a == f'[{ME} (you) -> Group "Comp"] Bet', a)
    check("group incoming keeps the real sender",
          b == '[Ethan Louie -> Group "Comp"] Ready in 5', b)
    check("the group label is never dropped",
          'Group "Comp"' in a and 'Group "Comp"' in b)


def test_addressee_annotation_survives() -> None:
    """The older @mention guard must still fire on top of the new shape."""
    print("\nthe @mention addressee tag still rides along")
    rows = [(1_754_500_000.0, "Group of 3 (Mom, Chetan, Trishe)",
             "Mom: @Trishe - Your post has 439 likes")]
    out = render(rows)[0]
    check("sender still leads", out.startswith("[Mom -> Group of 3"), out)
    check("addressee is still spelled out", "addressed to Trishe" in out, out)
    check("and it still says whose 'your' that is", "NOT the user" in out, out)


def test_unparseable_row_keeps_context() -> None:
    print("\na row with no `Sender:` is left attributed to its conversation")
    out = render([(1_754_500_000.0, "Mom", "no sender here")])[0]
    check("falls back to the conversation prefix", out == "Mom | no sender here", out)


def test_rules_are_shape_specific() -> None:
    print("\nattribution_rules emits ONE shape, and demands to be told which")
    directed = I.attribution_rules("directed")
    verbatim = I.attribution_rules("verbatim")
    check("directed teaches the arrow", "[Sender -> Recipient] text" in directed)
    check("directed does NOT teach the `Me` shape", "`Me`" not in directed)
    check("verbatim teaches the `Me` shape", "`Me`" in verbatim)
    check("verbatim does NOT teach the arrow",
          "[Sender -> Recipient] text" not in verbatim)
    # Shared rules (groups, borrowed 'you') belong to both.
    for name, txt in (("directed", directed), ("verbatim", verbatim)):
        check(f"{name} keeps the group-label rule", "FOUR-person conversation" in txt)

    try:
        I.attribution_rules("arrows")
    except ValueError:
        check("an unknown shape raises rather than silently emitting nothing", True)
    else:
        check("an unknown shape raises rather than silently emitting nothing", False)


def test_consumers_pick_the_matching_shape() -> None:
    """Wiring: the verbatim consumers must not be handed arrow rules."""
    print("\neach consumer asks for the shape it actually feeds")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def src(path: str) -> str:
        with open(os.path.join(root, path)) as f:
            return f.read()

    check("the agent loop (relays view_messages) asks for verbatim",
          'shape="verbatim"' in src("service/agent/loop.py"))
    # brief + message summarizer feed render_for_summary, so they take the
    # "directed" default; assert they did not opt into verbatim by mistake.
    for path in ("service/assistant/brief.py", "service/tools/imessage_tools.py"):
        check(f"{os.path.basename(path)} stays on the directed default",
              'shape="verbatim"' not in src(path))


def main() -> int:
    test_outgoing_one_to_one()
    test_no_name_colon_substring()
    test_incoming_and_groups()
    test_addressee_annotation_survives()
    test_unparseable_row_keeps_context()
    test_rules_are_shape_specific()
    test_consumers_pick_the_matching_shape()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
