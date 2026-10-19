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
