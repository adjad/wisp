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

Added 2026-09-08 after a live false positive: a two-week calendar digest the
user had ALREADY approved on the confirmation card was refused at the last
step, because two event titles copied verbatim out of get_upcoming carry
square brackets of their own ("Move-in [Adi Jain]"). Two things must hold now:

  * real bracketed content is not mistaken for an unfilled template slot,
    while "[Your Name]" still is;
  * the content heuristics never fire on text a human has already read and
    approved — they run BEFORE the card (outbound_content_problem), not
    behind it.

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


# The two event titles from the live failure (wisp-debug-2026-09-08_20-45-46).
REAL_CALENDAR_TEXT = (
    "Adi Jain’s schedule — next 14 days\n"
    "- Thu Sep 17, 8:15 AM — Move-in [Adi Jain] @ UCSC (Google)\n"
    "- Fri Sep 18, 5:30 PM — Community Meetings with your RA (various "
    "times) [MANDATORY for On-Campus Frosh] with 9/JRL Events Calendar")


def test_placeholders_vs_real_bracketed_content() -> None:
    print("\n_unfilled_placeholders: template slots yes, real content no")
    holes = action_tools._unfilled_placeholders(REAL_CALENDAR_TEXT)
    check("the calendar digest that was wrongly refused is now clean",
          holes == [], holes)
    check("a person's name in brackets is not a slot",
          action_tools._unfilled_placeholders("Move-in [Adi Jain]") == [])
    check("a bracketed event tag is not a slot",
          action_tools._unfilled_placeholders("[MANDATORY for On-Campus Frosh]") == [])
    check("[Your Name] is still caught",
          action_tools._unfilled_placeholders("Best,\n[Your Name]") == ["[Your Name]"])
    check("[insert date] is still caught",
          action_tools._unfilled_placeholders("See you [insert date].")
          == ["[insert date]"])
    check("{recipient} is still caught",
          action_tools._unfilled_placeholders("Hi {recipient},") == ["{recipient}"])
    check("<company> is still caught",
          action_tools._unfilled_placeholders("from <company>") == ["<company>"])


def test_preflight_runs_before_the_card() -> None:
    print("\noutbound_content_problem: the check the card is gated on")
    check("a real calendar digest raises nothing",
          action_tools.outbound_content_problem(
              "send_message", {"to": "Mom", "text": REAL_CALENDAR_TEXT}) is None)
    problem = action_tools.outbound_content_problem(
        "send_email", {"to": "a@b.com", "subject": "Hi", "body": "Best,\n[Your Name]"})
    check("a genuine unfilled slot is caught before the card",
          problem is not None and "[Your Name]" in problem, problem)
    check("a non-outbound tool is not this function's business",
          action_tools.outbound_content_problem("view_emails", {"body": "[Your Name]"})
          is None)


def test_approved_text_is_not_second_guessed() -> None:
    print("\nsend_message: a human who read the text outranks the heuristics")
    calls: list[dict] = []

    async def fake_request(event_type, payload, **kw):
        calls.append(payload)
        return {"ok": True}

    orig = action_tools.app_request
    action_tools.app_request = fake_request
    try:
        # Unapproved: the guard still does its job.
        blocked = asyncio.run(action_tools.send_message(
            "+17073171671", "Best,\n[Your Name]"))
        check("an unreviewed draft with a real slot is still refused",
              "NOT sent" in blocked, blocked)
        check("nothing was handed to the app", not calls, calls)

        # Approved on a card that showed the exact text: it sends.
        with action_tools.human_reviewed_content():
            sent = asyncio.run(action_tools.send_message(
                "+17073171671", "Best,\n[Your Name]"))
        check("an approved draft sends anyway", "Message sent" in sent, sent)
        check("the app got the text the user approved, unaltered",
              calls and calls[0]["text"] == "Best,\n[Your Name]", calls)
    finally:
        action_tools.app_request = orig


def main() -> int:
    test_degarble_literal_escapes()
    test_looks_truncated()
    test_placeholders_vs_real_bracketed_content()
    test_preflight_runs_before_the_card()
    test_approved_text_is_not_second_guessed()
    test_send_message_blocks_truncated_draft()
    test_send_message_degarbles_before_sending()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
