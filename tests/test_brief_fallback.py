"""Daily-brief fallback guard — regression tests.

Reported live (Wisp debug export, 2026-08-07 00:48): the "Daily summary" button
answered with the model's own PROMPT — "CALENDAR — TODAY IS Friday, August 7,
2026.", "ON THE CALENDAR TODAY (4 item(s)) — these and ONLY these are today's:",
"EMAIL (recent inbox, sender | subject):" — followed by every raw row. The same
session's "whats on my emails" and "and my messages" came back polished, which
is what made the brief look broken by comparison.

Mechanism (reproduced against the live engine, not inferred): the `fast` model
always opens a <think> block. The brief is the heaviest prompt on that path —
three marked sections over calendar + email + messages at once — and at
max_tokens=2000 it drafted the entire brief INSIDE the think block and hit the
ceiling before closing it. Measured 4/4 runs: finish_reason="length", ~7.3k
chars of reasoning, empty content. `_demote_unclosed_think` blanks that content
by design, so `_split_brief` took its fallback — which was `context`, the
prompt itself. At max_tokens=4000 the same prompt lands at ~1.6-3.1k completion
tokens and stops cleanly (3/3).

What must keep holding:
  * the brief's token ceiling stays at least as high as the summarizers' on the
    same model — that headroom is the actual fix;
  * an empty model response never renders prompt scaffolding to the user;
  * a real model response is passed through untouched.

    .venv/bin/python tests/test_brief_fallback.py
"""
from __future__ import annotations

import contextlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.assistant import brief as B  # noqa: E402

PASS, FAIL = 0, 0

# Lines that only ever appear in the prompt written FOR the model. If any of
# these reaches the user, the scaffold leaked.
SCAFFOLD = ("ON THE CALENDAR TODAY", "EMAIL (recent inbox", "ONLY these are today",
            "MESSAGES (recent iMessage", "do NOT invent", "LATER THIS WEEK")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def test_empty_response_uses_the_given_fallback() -> None:
    print("\nan empty model response falls back, and never to the prompt")
    sections = B._split_brief("", fallback="RENDERED")
    check("empty content takes the fallback", sections["FULL"] == "RENDERED")

    # The exact shape of the live failure: content blanked by the think guard,
    # and whitespace-only content must take the same path rather than rendering
    # as a blank brief.
    sections = B._split_brief("   \n  ", fallback="RENDERED")
    check("whitespace-only content also falls back", sections["FULL"] == "RENDERED")
    check("the fallback carries no prompt scaffolding",
          not any(s in sections["FULL"] for s in SCAFFOLD))


def test_real_response_is_untouched() -> None:
    print("\na real response is passed through, markers split out")
    raw = ("===TODAY===\nFour meetings today.\n\n"
           "===MESSAGES===\nEthan can join at 11:30.\n\n"
           "===FULL===\nGood morning! ☀️\n\n**📅 Today**\n- 10:00 AM run")
    s = B._split_brief(raw, fallback="RENDERED")
    check("TODAY split out", s["TODAY"] == "Four meetings today.")
    check("MESSAGES split out", s["MESSAGES"] == "Ethan can join at 11:30.")
    check("FULL is the model's prose, not the fallback",
          s["FULL"].startswith("Good morning!") and "RENDERED" not in s["FULL"])

    # A response with no markers at all is still the model's own answer.
    s = B._split_brief("Just a plain brief.", fallback="RENDERED")
    check("unmarked response is kept over the fallback",
          s["FULL"] == "Just a plain brief.")


@contextlib.contextmanager
def _stub_sources(now: float, store_mod, mail_mod, msg_mod):
    """Calendar / mail / message sources, stubbed — no engine, no user data.

    The mail stub goes through the REAL parser rather than returning a
    hand-written tuple, and that is the whole point of it existing. The previous
    version of this test faked `_parse_lines` as
    `[(now, "iCloud", "Kaggle", "Competition Launch")]` — four fields, the shape
    the cache had when the test was written. Mail sync later added a read/unread
    flag and the live rows became five-tuples, but the stub kept handing the
    brief four, so this file stayed green for days while EVERY press of the
    Daily Summary button raised ValueError in _email_block.

    Feeding a raw cache line through email_tools' own parser means the stub
    cannot drift from production: change the line format and the parser changes
    with it, or the test breaks honestly.
    """
    real = (store_mod.assistant_store.upcoming, mail_mod._headers,
            msg_mod._parse_lines, msg_mod.render_for_summary)
    store_mod.assistant_store.upcoming = lambda now=0, days=7: [
        {"when_ts": now + 3600, "title": "Go on a run", "kind": "meeting",
         "context": "Adi Jain", "account": "iCloud"}]
    mail_mod._headers = f"{now} | U | iCloud | Kaggle | Competition Launch"
    # (ts, context, "Sender: body") — three fields, the shape imessage_tools
    # actually returns. This stub said four for as long as the mail one said
    # four, and hid the same class of break in _plain_messages_section.
    msg_mod._parse_lines = lambda: [(now, 'Group "Comp"', "Ethan Louie: Ready in 5")]
    msg_mod.render_for_summary = lambda rows: ['Group "Comp" | Ethan Louie: Ready in 5']
    try:
        yield
    finally:
        (store_mod.assistant_store.upcoming, mail_mod._headers,
         msg_mod._parse_lines, msg_mod.render_for_summary) = real


def test_prompt_blocks_survive_the_real_cache_shape() -> None:
    """The regression that actually broke the button: every block builder must
    run against rows the live parser produced, not against a stub's idea of
    them."""
    print("\nthe prompt blocks build over real parsed rows")
    import service.assistant.store as store_mod
    import service.tools.email_tools as mail_mod
    import service.tools.imessage_tools as msg_mod

    now = 1_754_500_000.0
    with _stub_sources(now, store_mod, mail_mod, msg_mod):
        for name, fn in (("_calendar_block", B._calendar_block),
                         ("_email_block", B._email_block),
                         ("_messages_block", lambda n: B._messages_block()),
                         ("_plain_brief", B._plain_brief),
                         ("_plain_messages_section", B._plain_messages_section)):
            try:
                out = fn(now)
                check(f"{name} builds", bool(out.strip()))
            except Exception as e:  # noqa: BLE001
                check(f"{name} builds", False, f"{type(e).__name__}: {e}")

        block = B._email_block(now)
        check("the email block carries the mail through", "Kaggle" in block)
        # The • unread marker is the model's only signal for read status, and
        # the row above is flagged U.
        check("an unread row is marked", "• " in block)


def test_no_positional_unpacking_of_the_mail_cache() -> None:
    """The bug twice over: this module unpacked email_tools' row tuple
    positionally, and the tuple grew a field twice. Rows come in by name now
    (email_tools.header_rows), and this keeps it that way."""
    print("\nthe brief never unpacks the mail cache positionally")
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service/assistant/brief.py")).read()
    check("brief.py does not import email_tools._parse_lines",
          "email_tools import _parse_lines" not in src
          and "email_tools import (_parse_lines" not in src)
    check("brief.py reads mail rows through header_rows",
          "header_rows" in src)


def test_prompts_contain_no_fake_material() -> None:
    """A skeleton the model can mistake for data gets copied out as data.

    Measured 2026-08-14: _MSG_SYS shipped with realistic sample sections and
    Agents-A1-4B reproduced them verbatim in a real brief, inventing a party and
    a swimsuit that appear in none of the user's texts. Both prompts use
    <angle-bracket> slots now, and neither may reintroduce a worked example.
    """
    print("\nthe prompts show slots, not sample content")
    for name, prompt in (("_MSG_SYS", B._MSG_SYS), ("_BRIEF_SYS", B._BRIEF_SYS)):
        # A bold header in the skeleton must be a slot or a literal section
        # name from the brief's own fixed set — never a plausible contact.
        headers = re.findall(r"\*\*([^*\n]+)\*\*", prompt)
        allowed = ("<", "📅", "📧", "🔭", "💬", "needs a reply", "bold")
        bad = [h for h in headers if not any(a in h for a in allowed)]
        check(f"{name} has no sample-content headers", not bad, f"found {bad}")


def test_prompt_glyphs_never_reach_the_user() -> None:
    """The markers the prompt uses to label rows are for the model to read."""
    print("\nprompt glyphs are stripped from model output")
    out = B._strip_prompt_glyphs("• Arnav M wants to connect\n- • Mom — hi")
    check("a leading unread dot becomes a normal bullet",
          "•" not in out and out.startswith("- Arnav"), out)
    check("a dotted list item stays one bullet", out.endswith("- Mom — hi"), out)
    kept = B._strip_prompt_glyphs("The score was 3 • 2 in the end.")
    check("a mid-sentence dot is left alone", "3 • 2" in kept, kept)
    out = B._strip_prompt_glyphs("- [Mom -> Adi] hello")
    check("a routing marker is still stripped", out == "- hello", out)


def test_leaked_placeholder_instructions_never_reach_the_user() -> None:
    """_strip_leading_placeholder_leaks — a different failure than the bracketed
    greeting it grew out of.

    Reported live 2026-08-15 (Daily Summary, ~1/8 runs): the brief's text
    started with "2-4 short lines, PLAIN TEXT, for a phone notificat..." — the
    literal ===TODAY=== placeholder from _BRIEF_SYS, copied into the ===FULL===
    greeting slot instead of a real greeting. Reproduced against the live
    engine (20 scripted stage-two calls over a busy synthetic day, to lengthen
    the prompt the way _messages_rundown's own measurements show degrades this
    model): 2/20 runs. One run leaked that ===TODAY=== placeholder alone; a
    second leaked it AND a second, paraphrased line ("<3 line greeting with one
    emoji>" — not a verbatim copy of anything in the prompt, but still
    describing the slot instead of filling it).

    The old `_BRACKETED_GREETING` only handled the OTHER case — real content
    the model wrapped in brackets it forgot to strip (e.g. "<Good afternoon
    ☀️>") — and blindly debracketed anything at the front of a section. Against
    the leak above that turned invisible brackets into visible instruction text
    reaching the user, which is the exact bug this guards.
    """
    print("\nleaked placeholder instructions are dropped, real greetings kept")

    # The exact live failure: the TODAY placeholder copied into FULL's slot.
    leaked_today_placeholder = (
        "<2-4 short lines, PLAIN TEXT, for a phone notification>\n\n"
        "**📅 Today**\nFour things on the calendar today.")
    out = B._strip_prompt_glyphs(leaked_today_placeholder)
    check("the leaked placeholder text is gone", "PLAIN TEXT" not in out
          and "phone notification" not in out, out)
    check("the section content after it survives",
          out == "**📅 Today**\nFour things on the calendar today.", out)

    # The exact live failure: TWO leaked lines back to back, nothing real left.
    both_leaked = ("<2-4 short lines, PLAIN TEXT, for a phone notification>\n\n"
                   "<3 line greeting with one emoji>")
    out = B._strip_prompt_glyphs(both_leaked)
    check("both leaked lines are dropped, leaving nothing rather than "
          "instruction text", out == "", out)

    # A real greeting the model wrapped in brackets is still unwrapped, not
    # dropped — the original bug this backstop must not reintroduce.
    real_greeting = "<Good afternoon ☀️>\n\n**📅 Today**\nNothing on today."
    out = B._strip_prompt_glyphs(real_greeting)
    check("a real bracketed greeting is unwrapped, not deleted",
          out == "Good afternoon ☀️\n\n**📅 Today**\nNothing on today.", out)

    # A real greeting immediately followed by the header, no blank line
    # between them — the blank-line handling must not depend on one existing.
    no_gap = "<Good afternoon ☀️>\n**📅 Today**\nNothing on today."
    out = B._strip_prompt_glyphs(no_gap)
    check("a real greeting with no trailing blank line still unwraps cleanly",
          out == "Good afternoon ☀️\n**📅 Today**\nNothing on today.", out)


def test_message_sections_are_deduped_and_deiconed() -> None:
    """_clean_message_sections — the backstop behind _MSG_SYS's header rules.

    Reproduced live 2026-08-14 against a real 3-block conversation set (one
    group chat, one small group, one small group), 5 raw runs at temperature
    default: run 1 prefixed EVERY header with '⏰' ("**⏰ 👥 Group of 3...**"),
    run 3 emitted the group "Grad GC" as two sections — its own block, then a
    second one for a single member's phone number pulled out of it. Both
    violate _MSG_SYS's stated shape, and asking is not guaranteeing, so this is
    enforced in Python on the model's actual output rather than left to the
    prompt alone."""
    print("\n_clean_message_sections is a backstop, not just a prompt request")
    leaked_icon = ('**⏰ 👥 Group of 3 (Mom, dad, Trishe)**\nMom and Trishe went '
                   'back and forth about cupcakes.\n\n'
                   '**👥 Group "Comp"**\nEthan shared a video.')
    out = B._clean_message_sections(leaked_icon)
    check("a time icon is removed from a header",
          "⏰" not in out.split("\n")[0], out)
    check("the header still names the conversation",
          "Group of 3" in out.split("\n")[0], out)
    check("the body is untouched", "Mom and Trishe" in out)

    split_block = ('**👥 Group "Grad GC"**\nThe group asked about running it '
                   'again tonight.\n\n'
                   '**⏰ Group "Grad GC"**\nThat number is asking about tonight.')
    out = B._clean_message_sections(split_block)
    headers = [l for l in out.splitlines() if l.strip().startswith("**")]
    check("a block split into two sections collapses to one",
          len(headers) == 1, headers)
    check("the surviving section keeps its real content",
          "asked about running it again" in out, out)

    exact_repeat = ('**👥 Mom**\nShe asked if you were awake.\n\n'
                    '**👥 Mom**\nShe wished you luck.')
    out = B._clean_message_sections(exact_repeat)
    check("an exact duplicate header keeps only the first telling",
          out.count("**👥 Mom**") == 1 and "awake" in out and "luck" not in out,
          out)

    distinct = ('**👩 Mom**\nShe asked if you were awake.\n\n'
               '**👥 Grad GC**\nPlanning to play a game tonight.')
    out = B._clean_message_sections(distinct)
    check("two genuinely different conversations both survive",
          out.count("**") == 4, out)


def test_plain_brief_renders_no_scaffold() -> None:
    """_plain_brief over stubbed sources — no engine, no user data needed."""
    print("\n_plain_brief renders the sources, not the prompt")
    import service.assistant.store as store_mod
    import service.tools.email_tools as mail_mod
    import service.tools.imessage_tools as msg_mod

    now = 1_754_500_000.0  # fixed clock so the rows are deterministic
    with _stub_sources(now, store_mod, mail_mod, msg_mod):
        out = B._plain_brief(now)

    leaked = [s for s in SCAFFOLD if s in out]
    check("no prompt scaffolding reaches the user", not leaked, f"leaked {leaked}")
    check("the calendar item is still there", "Go on a run" in out)
    check("the email is still there", "Kaggle" in out)
    check("the message is still there", "Ready in 5" in out)
    check("it is formatted like the brief, not a dump",
          "**📅 Today**" in out and "**📧 Inbox**" in out)


def test_token_ceiling_has_headroom() -> None:
    """The trigger, guarded at the source: the brief must not be capped below
    the summarizers it shares a model with."""
    print("\nthe brief's token ceiling keeps its headroom")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def cap(path: str) -> int:
        """Highest completion ceiling a file can request.

        Matches the named constant as well as the literal. Both summarizers
        moved to `_SUMMARY_MAX_TOKENS` after this test was written, and a regex
        for `max_tokens=\\d+` alone quietly found nothing in either — so the two
        comparisons below were 5000 >= 0, passing no matter what the summarizers
        did. A guard that cannot fail is not a guard.
        """
        with open(os.path.join(root, path)) as f:
            src = f.read()
        found = re.findall(r"max_tokens=(\d+)", src)
        found += re.findall(r"^_SUMMARY_MAX_TOKENS\s*=\s*(\d+)", src, re.MULTILINE)
        return max(int(n) for n in found) if found else 0

    brief_cap = cap("service/assistant/brief.py")
    mail_cap = cap("service/tools/email_tools.py")
    # imessage_tools imports the constant from email_tools rather than
    # redefining it, so the two summarizers share one ceiling.
    msg_cap = mail_cap
    check(f"brief cap ({brief_cap}) >= email summarizer ({mail_cap})",
          brief_cap >= mail_cap)
    check(f"brief cap ({brief_cap}) >= message summarizer ({msg_cap})",
          brief_cap >= msg_cap)
    # 2000 is the measured-failing value, not an arbitrary floor.
    check("brief cap is above the value that truncated 4/4", brief_cap > 2000)


def test_the_button_never_raises() -> None:
    """build_daily_brief is what the endpoint awaits, and a raise there is a 500
    the app can only render as "Couldn't build a summary right now." — the exact
    shape of the failure this file now guards. A dead source must degrade to the
    rendered brief instead."""
    print("\nthe Daily Summary entry point survives a dead source")
    import asyncio
    import service.tools.email_tools as mail_mod

    def boom():
        raise ValueError("too many values to unpack (expected 4, got 5)")

    real = mail_mod._parse_lines
    mail_mod._parse_lines = boom
    try:
        out = asyncio.run(B.build_daily_brief("morning"))
    finally:
        mail_mod._parse_lines = real
    check("a brief still comes back", bool(out.strip()))
    check("and it isn't a traceback or prompt scaffolding",
          "Traceback" not in out and not any(s in out for s in SCAFFOLD))


def main() -> int:
    test_empty_response_uses_the_given_fallback()
    test_real_response_is_untouched()
    test_prompt_blocks_survive_the_real_cache_shape()
    test_no_positional_unpacking_of_the_mail_cache()
    test_prompts_contain_no_fake_material()
    test_prompt_glyphs_never_reach_the_user()
    test_leaked_placeholder_instructions_never_reach_the_user()
    test_message_sections_are_deduped_and_deiconed()
    test_plain_brief_renders_no_scaffold()
    test_the_button_never_raises()
    test_token_ceiling_has_headroom()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
