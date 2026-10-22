"""The Day's review blocks: mass mail never becomes one, an email about a
calendar event never doubles it, and several reviews share one readable block.

From the user's Day on 2026-10-19: seven dashed "Review: …" blocks stacked on
one another at noon, nearly all newsletters and event blasts (SF Tech Week,
"You're subscribed", a BYU course ad), plus an email twin of a calendar event
and a Canvas notification digest. Synthetic rows only; no native sources.
"""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-day-review-blocks-tests-")
os.environ["WISP_HOME"] = _scratch.name

from service.assistant import behavior_sources
from service.assistant.daily_workspace import (_event_key, _names_event, assess, important_email_rows,
                                               mass_mail_filter, not_a_need)

DAY, ZONE = "2026-10-19", "America/Los_Angeles"


def at(hour, minute=0):
    return datetime(2026, 10, 19, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()


NOW = at(9)


def mail(subject, sender, address, *, message_id="<m@example.org>", unread=True, body=""):
    return dict(subject=subject, sender=f"{sender} <{address}>", sender_address=address,
                message_id=message_id, unread=unread, body=body, ts=NOW - 600,
                account_id="acct", account="me@example.edu")


TECH_WEEK = mail("LA Tech Week starts today", "Katia Ameri", "hello@tech-week.com")
SUBSCRIBED = mail("You’re subscribed: The AI Builder’s Briefing", "Luis", "luis@mindstudio.ai")
CANVAS_DIGEST = mail("Recent Canvas Notifications", "Canvas", "notifications@instructure.com")
CANVAS_ACTION = mail("PA 2 has been posted: Beginning Programming in Python", "Canvas",
                     "notifications@instructure.com")
PERSON = mail("Re: Week 4 attendance question", "Devarsh Patel", "devarsh@ucsc.edu")


def test_mass_mail_and_digests_are_not_needs():
    assert not_a_need(TECH_WEEK)      # a campaign mailbox under a person's name (#299's rule)
    assert not_a_need(SUBSCRIBED)     # signing up for a list
    assert not_a_need(CANVAS_DIGEST)  # a roll-up of notifications that arrive on their own


def test_people_and_course_actions_still_are():
    assert not not_a_need(PERSON)
    assert not not_a_need(CANVAS_ACTION)
    # digest_category's "news" is also its fallback, so mail it can't place still counts.
    assert not not_a_need(dict(subject="Native", body="Independent native evidence."))


def test_every_copy_of_flagged_mail_is_flagged():
    # The same BYU course ad synced through two accounts: only one copy carries
    # the Mailchimp Message-ID that marks it as list mail.
    ad = "BIOL 043: Flexible Science Credit For Your Teen"
    marked = mail(ad, "BYU Independent Study", "indstudy@byu.edu", message_id="<abc@mail123.mcsv.net>")
    bare = mail(ad, "BYU Independent Study", "indstudy@byu.edu", message_id="<18f2c@mail.gmail.com>")
    assert not not_a_need(bare)
    skip = mass_mail_filter([marked, bare, PERSON])
    assert skip(marked) and skip(bare) and not skip(PERSON)


def test_day_email_evidence_drops_mass_mail():
    snapshot = SimpleNamespace(rows=[TECH_WEEK, SUBSCRIBED, CANVAS_DIGEST, CANVAS_ACTION, PERSON])
    kept = [row["subject"] for row, _ in important_email_rows(snapshot)]
    assert TECH_WEEK["subject"] not in kept and SUBSCRIBED["subject"] not in kept
    assert CANVAS_DIGEST["subject"] not in kept
    assert PERSON["subject"] in kept and CANVAS_ACTION["subject"] in kept


def test_behavior_pass_never_reads_mass_mail_as_possible_work():
    skip = behavior_sources._native_filter([TECH_WEEK, PERSON])
    assert skip(TECH_WEEK) and not skip(PERSON)
    # Gmail history has no sender: its header is found by Message-ID, or its own
    # Message-ID or list footer gives it away.
    record = lambda message_id, text="Hi, can you send the notes?": SimpleNamespace(
        rfc_message_id=message_id, body=SimpleNamespace(text=text))
    assert behavior_sources._gmail_not_a_need(record("<Tech@Week>"), {"tech@week"})
    assert behavior_sources._gmail_not_a_need(record("<x@mail9.mcsv.net>"), set())
    assert behavior_sources._gmail_not_a_need(record("<p@q>", "News\nUnsubscribe from this list"), set())
    assert not behavior_sources._gmail_not_a_need(record("<p@q>"), set())
    # A list footer in quoted history below the reply is not this email's own text.
    assert not behavior_sources._gmail_not_a_need(record("<p@q>", "Sounds good.\n> Unsubscribe here"), set())


def test_an_email_about_a_calendar_event_matches_it():
    events = {_event_key("Dublin Community Office Hours")}
    assert _event_key("Invitation: Office Hours @ Sat Oct 24, 2026 2pm") == "office hours"
    assert _names_event("Dublin Community Office Hours with Councilmember Morada [In-person]", events)
    assert _names_event("Review: Dublin Community Office Hours with Councilmember Morada [In", events)
    assert not _names_event("Week 4 attendance question", events)
    # A short event title never swallows unrelated work by prefix alone.
    assert not _names_event("Lunch with the design team", {_event_key("Lunch")})


def state(commitments=()):
    return dict(tasks=[], commitments=list(commitments),
                preferences=dict(start_minute=540, end_minute=1200, not_before=None),
                sources={s: dict(available=True, last_sync=NOW, snapshot_started_at=NOW,
                                 diagnostics=dict(coverage_start=at(0), coverage_end=at(23, 59) + 60))
                         for s in ("calendar", "reminders")})


def email(identity, title, sender):
    return dict(id=identity, title=title, quote=title, sender=sender, person=True,
                review_reason="Review this source for requests or changes.")


def run(records, commitments=()):
    evidence = dict(email=dict(ready=True, records=records, detail="Synthetic bounded source"))
    return assess(DAY, ZONE, state(commitments), evidence, now=NOW)


def suggestions(result):
    return [b for b in result["proposed_plan"]["blocks"] if b["kind"] == "suggestion"]


def test_several_reviews_share_one_block_listing_them():
    result = run([email("a", "Week 4 attendance question", "Devarsh Patel"),
                  email("b", "Lab partner sign-up", "Maya Chen"),
                  email("c", "Club budget", "Sam Ortiz")])
    blocks = suggestions(result)
    assert len(blocks) == 1
    block = blocks[0]
    # Members follow the planner's own ranking; the title names the first two senders.
    names = [m["sender"] for m in block["members"]]
    assert block["title"] == f"Review 3 emails · {names[0]}, {names[1]} +1"
    assert sorted(m["title"] for m in block["members"]) == ["Club budget", "Lab partner sign-up", "Week 4 attendance question"]
    assert {m["evidence_id"] for m in block["members"]} == {i["id"] for i in result["attention"]}
    # Long enough to read: never a stack of 15-minute slivers.
    assert block["end"] - block["start"] == 30 * 60


def test_a_single_review_keeps_its_own_title():
    blocks = suggestions(run([email("a", "Week 4 attendance question", "Devarsh Patel")]))
    assert [b["title"] for b in blocks] == ["Review: Week 4 attendance question"]
    assert "members" not in blocks[0]


def test_an_invitation_email_is_not_also_a_review_block():
    event = dict(id="cal:dublin", source="calendar", kind="meeting", title="Dublin Community Office Hours",
                 when_ts=at(18), end_ts=at(19), all_day=False)
    result = run([email("a", "Dublin Community Office Hours with Councilmember Morada [In-person]", "John Morada"),
                  email("b", "Week 4 attendance question", "Devarsh Patel")], [event])
    assert [b["title"] for b in suggestions(result)] == ["Review: Week 4 attendance question"]
    # It still shows as something that needs you; only the duplicate block goes.
    assert any(i["title"].startswith("Dublin Community") for i in result["attention"])


# The 2026-10-20 screenshot: under "Wisp live reminder test 2102", six "Review: …"
# blocks stacked again. A sign-in email (its subject withheld as "Authentication
# details omitted.") and the Canvas digest go; the course mail that is left shares
# one block beside the task.
CANVAS, ED = "notifications@instructure.com", "notification@edstem.org"
SCREENSHOT = [
    mail("Alcohol Edu Part 2 : Alcohol Edu Survey Part 2", "Canvas", CANVAS),
    mail("Recent Canvas Notifications", "Canvas", CANVAS),
    mail("New in 26 Fall-STAT 5 02: Section Notes (Updated Regularly)", "Ed Discussion", ED),
    mail("New assignments have been posted // PA 2 grades have been posted", "Canvas", CANVAS),
    mail("New in CSE 20 04: Homework error", "Ed Discussion", ED),
    mail("Your verification code is 482913", "Google", "no-reply@accounts.google.com"),
]


def test_withheld_sign_in_mail_is_never_a_need():
    assert not_a_need(SCREENSHOT[5])
    assert not_a_need(mail("Sign in to Canvas: 123456 is your code", "Canvas", CANVAS))
    # A forum's roll-up is a digest; a single post in it is not.
    assert not_a_need(mail("New threads in 26 Fall-STAT 5 02", "Ed Discussion", ED))
    assert not not_a_need(SCREENSHOT[4])


def test_the_screenshot_leaves_one_review_block_beside_the_task():
    skip = mass_mail_filter(SCREENSHOT)
    kept = [row for row in SCREENSHOT if not skip(row)]
    assert [r["subject"] for r in kept] == [SCREENSHOT[i]["subject"] for i in (0, 2, 3, 4)]
    records = [email(f"e{i}", r["subject"], r["sender"].split(" <")[0]) for i, r in enumerate(kept)]
    s = state()
    s["tasks"] = [dict(id="t2102", title="Wisp live reminder test 2102", day=DAY, kind="task",
                       duration_minutes=30, priority=2, status="active", due_ts=None,
                       pinned_start=at(12), revision=1)]
    evidence = dict(email=dict(ready=True, records=records, detail="Synthetic bounded source"))
    blocks = assess(DAY, ZONE, s, evidence, now=NOW)["proposed_plan"]["blocks"]
    reviews = [b for b in blocks if b["kind"] == "suggestion"]
    assert len(reviews) == 1, [b["title"] for b in reviews]
    assert reviews[0]["title"].startswith("Review 4 emails · ")
    assert sorted(m["title"] for m in reviews[0]["members"]) == sorted(r["subject"] for r in kept)
    # Nothing on the plan overlaps anything else.
    timed = sorted((b["start"], b["end"], b["title"]) for b in blocks if b.get("end") is not None)
    assert all(a[1] <= b[0] for a, b in zip(timed, timed[1:])), timed


# Every stand-in imessage_tools.redact_summary_codes emits for a withheld subject.
# A credential's goes (not_a_need); every other keeps its class as a need, named
# by class and sender, never "Review: Private details omitted."
from service.assistant.daily_workspace import review_title

PLACEHOLDER_TITLES = {
    "Private details omitted.": "Review an email from Dr. Patel",
    "Possible request context (private details omitted; review original message).":
        "Review a possible request from Dr. Patel",
    "Health or safety concern (private details omitted).": "Review a health or safety notice from Dr. Patel",
    "Deadline notice (private details omitted).": "Review a deadline notice from Dr. Patel",
    "Schedule or logistics change (private details omitted).": "Review a schedule change from Dr. Patel",
    "Direct request with a stated deadline (private details omitted).":
        "Review a request with a deadline from Dr. Patel",
    "Please review the original request with a stated deadline by Friday (private details omitted).":
        "Review a request with a deadline from Dr. Patel",
    "Direct request requires review. Private details omitted.": "Review a request from Dr. Patel",
    "Please review the original request. Private details omitted.": "Review a request from Dr. Patel",
    "Unverified security incident notice (details omitted).": "Check a security notice from Dr. Patel (unverified)",
}
CREDENTIAL_TITLES = (
    "Authentication details omitted.",
    "Direct request requires review. Authentication details omitted.",
    "Please review the original request. Authentication details omitted.",
)


def test_every_withheld_subject_is_titled_by_its_class_and_sender():
    for stand_in, expected in PLACEHOLDER_TITLES.items():
        assert review_title(stand_in, "Dr. Patel") == expected, stand_in
        # A partial stand-in keeps the same class.
        assert review_title("Canvas: " + stand_in, "Dr. Patel") == expected, stand_in
    assert review_title("Private details omitted.") == "Review an email"
    assert review_title("Week 4 attendance question", "Devarsh") == "Review: Week 4 attendance question"


def test_withheld_needs_stay_needs_and_credentials_go():
    for stand_in in PLACEHOLDER_TITLES:
        assert not not_a_need(mail(stand_in, "Dr. Patel", "patel@ucsc.edu")), stand_in
    for stand_in in CREDENTIAL_TITLES:
        assert not_a_need(mail(stand_in, "Dr. Patel", "patel@ucsc.edu")), stand_in


def test_a_security_notice_is_named_never_an_instruction():
    title = review_title("Unverified security incident notice (details omitted).", "Google")
    assert title == "Check a security notice from Google (unverified)"
    assert not any(word in title.lower() for word in ("reset", "sign in", "click", "verify your", "act now"))


def test_withheld_subjects_read_plainly_on_the_day():
    blocks = suggestions(run([email("a", "Deadline notice (private details omitted).", "Dr. Patel")]))
    assert [b["title"] for b in blocks] == ["Review a deadline notice from Dr. Patel"]
    grouped = suggestions(run([email("a", "Private details omitted.", "Dr. Patel"),
                               email("b", "Week 4 attendance question", "Devarsh Patel")]))
    assert len(grouped) == 1
    titles = sorted(m["title"] for m in grouped[0]["members"])
    assert titles == ["Review an email from Dr. Patel", "Week 4 attendance question"]
    assert not any("details omitted" in b["title"].lower() for b in grouped)
