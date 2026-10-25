"""Before you ask reads the automated mail that still needs the user, and its
reminder cards never offer a time that is already gone.

Real failures (2026-10-24, 132 mail rows, 3 items found):
- A course system's own action mail ("Makeup Quiz 2 // Reminder: Practice
  Midterm (10/26)", an Ed "Exam 1 next Wednesday" post, "Assignment Graded")
  is filed under "attention", not "school", so the school path never read it.
- A collaborator's comment in a shared Google doc, a GitHub account at its
  storage limit, and similar personal notices were dropped as machine mail.
- A Gradescope thread was titled by its privacy stand-in, "Private details omitted."
- A weekly Canvas roll-up became "Review recent Canvas Notifications" with
  "I've set up a reminder for tue oct 19 12:00 pm": a past entry ("due: Oct 19",
  mail from Oct 24) resolved into next year, echoed raw, claimed as done.

Synthetic rows only (sender organisations kept, no real people or addresses);
fake model; nothing native, nothing live.
"""
import asyncio
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

_scratch = tempfile.TemporaryDirectory(prefix="wisp-mail-recall-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service.proactive import guard, judge, prepare, sources, triage

TZ = "America/Los_Angeles"
ZONE = ZoneInfo(TZ)
NOW = datetime(2026, 10, 24, 18, 0, tzinfo=ZONE).timestamp()        # Sat Oct 24 2026, 6 PM
ME = "student@school.example.edu"
NAMES = ["Sam Student", "Sam"]
CANVAS = "notifications@instructure.com"
FOOTER = ("\n\n________________________________________\n\nYou received this email because you are participating "
          "in one or more classes using Canvas. To change or turn off email notifications, visit the settings page.")


@pytest.fixture(autouse=True)
def user_is_sam(monkeypatch):
    monkeypatch.setattr(triage, "user_names", lambda: list(NAMES))
    import service.memory.identity as identity
    monkeypatch.setattr(identity, "user_name", lambda: "Sam Student")
    monkeypatch.setattr(identity, "user_emails", lambda: [ME])


def mail(mid, sender, address, subject, body="", ago_h=3, **extra):
    return dict(ts=NOW - ago_h * 3600, account=ME, account_id="acct-1", sender=f"{sender} <{address}>",
                sender_address=address, to=ME, subject=subject, message_id=mid, body=body, unread=True) | extra


def items_for(rows):
    counts = {}
    got = sources.mail_items(rows, mine={ME}, now=NOW, tz=TZ, linked=set(), own="me", counts=counts)
    return got, counts


def titles(rows):
    return {i["title"] for i in items_for(rows)[0]}


# --- 1. course and school action mail -------------------------------------------------------


COURSE_ACTION = [
    mail("<a1@x>", "Beginning Programming in Python", CANVAS,
         "Makeup Quiz 2 // Reminder: Practice Midterm during the Monday Lecture (10/26): Beginning Programming in Python",
         "Hi everyone, you can request a makeup quiz 2 if you missed quiz 2. Reminder: the practice midterm is "
         "during Monday's lecture (10/26)." + FOOTER),
    mail("<a2@x>", "TA Name via Ed", "notification@edstem.org", "26 Fall-STAT 5 02: Reminder: STAT 5 Exam 1 Next Wednesday",
         "Course: 26 Fall-STAT 5 02 Dear all, a reminder that Exam 1 is next Wednesday during class. "
         "Edit your email preferences at the course page."),
    mail("<a3@x>", "Statistics", CANVAS, "Assignment Graded: HW2, Statistics",
         "Your assignment HW2 has been graded. You can review the assignment here." + FOOTER),
    mail("<a4@x>", "Course Instructor", CANVAS, "Course Instructor (Academic Writing)",
         "Subject: In-Class Writing Activity on Monday (attendance required) Hello everyone, attendance is "
         "mandatory on Monday." + FOOTER),
]


def test_a_course_systems_own_action_mail_is_read():
    got, counts = items_for(COURSE_ACTION)
    assert len(got) == 4
    assert all(i["recall"] == "school" and i["person"] is False for i in got)
    assert counts["me"]["filtered"] == 0


def test_a_submission_receipt_and_course_chatter_stay_out():
    rows = [mail("<r1@x>", "Gradescope", "no-reply@gradescope.com",
                 "[STAT5-02-2026 FALL on Gradescope] Successfully submitted to Worksheet 2",
                 "You've successfully submitted Worksheet 2. You may be able to resubmit until the due date."),
            mail("<r2@x>", "Beginning Programming in Python", CANVAS,
                 "Office hours today from 8:45AM to 10:45AM: Beginning Programming in Python",
                 "Hi everyone, I'll be having office hours today on Zoom." + FOOTER)]
    got, counts = items_for(rows)
    assert got == []
    assert counts["me"]["filtered"] == 2


def test_a_withheld_subject_is_titled_by_class_and_sender_never_by_the_stand_in():
    rows = [mail("<g1@x>", "Gradescope", "no-reply@gradescope.com",
                 "[STAT5-02-2026 FALL on Gradescope] Worksheet 3 has been graded",
                 "Your submission to Worksheet 3 has been graded. Regrade requests are due by Friday.")]
    got, _ = items_for(rows)
    assert len(got) == 1
    assert got[0]["title"] == "Gradescope: new course update"
    assert "details omitted" not in got[0]["title"].casefold()
    assert "details omitted" not in got[0]["text"].casefold()


def test_shown_title_keeps_an_ordinary_subject_and_names_a_withheld_class():
    assert sources.shown_title("Lunch on Friday?", "Pat") == "Lunch on Friday?"
    assert sources.shown_title("Private details omitted.", "Acme") == "Acme: new email"
    assert sources.shown_title("Canvas: Private details omitted.", "UCSC Canvas", school=True) == \
        "UCSC Canvas: new course update"
    assert sources.shown_title("Deadline notice (private details omitted).", "Bursar") == "Bursar: new deadline notice"


# --- 2. personal transactional mail and doc comments ---------------------------------------------


NOTICES = [
    mail("<n1@x>", "GitHub", "noreply@github.com",
         "[GitHub] You have used 100% of the Actions storage included for the sam account",
         "You have used 100% so far this billing cycle."),
    mail("<n2@x>", "City Dental", "noreply@dental.example.com", "Appointment confirmed for Tue, Oct 27 at 3:00 PM"),
    mail("<n3@x>", "Electric Co", "billing@power.example.com", "Your payment is due Oct 30"),
    mail("<n4@x>", "Consulting Club Recruiting", "noreply@club.example.org", "Your application status has been updated"),
]

STAY_OUT = [
    mail("<o1@x>", "Glassdoor Jobs", "noreply@glassdoor.com", "Cashier at Ace Hardware. 15 more cashier jobs in Dublin, CA"),
    mail("<o2@x>", "Indeed", "alert@jobalert.indeed.com", "Snack Bar Attendant at Hudson. Apply now"),
    mail("<o3@x>", "Apple", "no_reply@email.apple.com", "Your receipt from Apple."),
    mail("<o4@x>", "Facebook", "notification@priority.facebookmail.com", "Isaiah sent you a friend request, plus more updates"),
    mail("<o5@x>", "AI Briefing", "newsletter@briefing.example.com", "Reverse engineering everything, one model at a time",
         "Read more. Unsubscribe"),
    mail("<o6@x>", "Shop", "deals@shop.example.com", "Flash sale: your payment due date is extended! 40% off"),
    mail("<o7@x>", "Handshake", "jobs@g.joinhandshake.com", "Jobs for you like 2027 Summer Paid Health Internships"),
    mail("<o8@x>", "Major League Hacking", "hi@mlh.io", "Hacktoberfest x CruzHacks is starting!", "Unsubscribe"),
    mail("<o9@x>", "Vote.org", "info@vote.org", "Sam, GAME ON VOTERS! The two week countdown is here."),
]


def test_personal_notices_are_read_and_promos_jobs_receipts_social_stay_out():
    got, counts = items_for(NOTICES + STAY_OUT)
    assert {i["recall"] for i in got} == {"notice"}
    assert len(got) == len(NOTICES)
    assert counts["me"]["filtered"] == len(STAY_OUT)


def test_a_collaborators_doc_comment_is_a_persons_words_with_nothing_to_email_back():
    rows = [mail("<d1@x>", "Pat Partner (Google Slides)", "comments-noreply@docs.google.com", "Team Deck.pptx",
                 "New activity in the following document Team Deck.pptx 2 comments Pat Partner: can you add the "
                 "logo to slide 3?"),
            mail("<d2@x>", "Pat Partner (Google Slides)", "comments-noreply@docs.google.com", "Team Deck... - change",
                 "Pat Partner resolved a comment in the following document Team Deck.pptx Marked as resolved")]
    got, _ = items_for(rows)
    assert len(got) == 1
    item = got[0]
    assert item["who"] == "Pat Partner" and item["person"] is True and item["recall"] == "doc_comment"
    assert item["reply_ref"] is None


def test_a_persons_mail_is_still_a_person_not_a_recall():
    rows = [mail("<p1@x>", "Pat Partner", "pat@example.com", "Details", "Can you send me the details by Monday?")]
    got, _ = items_for(rows)
    assert got[0]["person"] is True and "recall" not in got[0]


# --- 3. a course roll-up is named by what in it is still due ----------------------------------------


def rollup(entries, ago_h=3):
    body = ("You're signed up to receive a weekly report of some notifications from your Canvas account. "
            "Below is the report for the week ending Oct 24:\n\n")
    for name, due in entries:
        body += f"--------------------------------\n\n Assignment Created - {name}\n\n due: {due}\n\n <link>\n"
    return mail("<w1@x>", "UCSC Canvas", CANVAS, "Recent Canvas Notifications", body + FOOTER, ago_h=ago_h)


def test_a_rollup_with_only_past_entries_is_no_item():
    got, counts = items_for([rollup([("Week4 Attendance Quiz, Beginning Programming in Python", "Oct 19 at 12pm"),
                                     ("Attendance October 21, Beginning Programming in Python", "Oct 21 at 4pm"),
                                     ("Discussion Week 3, Beginning Programming in Python", "No Due Date")])])
    assert got == []
    assert counts["me"]["filtered"] == 1


def rollup_item(sched=None, judged_title="Review recent Canvas Notifications", due="Oct 19 at 12pm"):
    got, _ = items_for([rollup([("Week4 Attendance Quiz, Beginning Programming in Python", "Oct 19 at 12pm"),
                                ("Worksheet 3, Statistics", "Oct 25 at 11:59pm"),
                                ("HW3 , Statistics", "Oct 27 at 11:59pm")])])
    assert len(got) == 1
    item = got[0]
    survivor = {"item": item, "result": {"title": "Canvas notifications", "summary": "", "asks_of_me": ""}}
    raw = {"title": judged_title, "action": "task", "why": "", "due": due, "urgency": "medium"}
    return item, judge._finalize(raw, {"kind": "item", "survivor": survivor}, [], now=NOW, tz=TZ,
                                 sched=sched or {"calendar": [], "reminders": []}, suppressed=set(), allowed=set())


def test_a_rollup_names_its_soonest_entry_still_due_never_a_past_one():
    item, judged = rollup_item()
    assert item["title"] == "UCSC Canvas: 2 items due soon"
    assert "Oct 19" not in item["text"]
    assert judged["title"] == "Finish Worksheet 3, Statistics"
    assert judged["due_ts"] == datetime(2026, 10, 25, 23, 59, tzinfo=ZONE).timestamp()
    assert "Notifications" not in judged["title"]


def test_a_rollup_entry_already_on_the_calendar_gives_way_to_the_next():
    sched = {"calendar": [{"title": "Worksheet 3 [STAT-5-02]", "when_ts": NOW + 3600}], "reminders": []}
    _, judged = rollup_item(sched=sched)
    assert judged["title"] == "Finish HW3, Statistics"


# --- 4. times: resolved from the mail's arrival, never in the past ------------------------------------


def test_a_yearless_date_just_before_the_mail_arrived_is_that_past_date_not_next_year():
    arrival = datetime(2026, 10, 24, 9, 0, tzinfo=ZONE).timestamp()
    due_ts, _, has_clock = guard.resolve_due("Oct 19 at 12pm", arrival, TZ)
    assert has_clock and due_ts == datetime(2026, 10, 19, 12, 0, tzinfo=ZONE).timestamp()
    ahead, _, _ = guard.resolve_due("Oct 30 at 12pm", arrival, TZ)
    assert ahead == datetime(2026, 10, 30, 12, 0, tzinfo=ZONE).timestamp()


def org_item(due_ts, has_clock=True, title="Finish Worksheet 3, Statistics"):
    return {"title": title, "who": "UCSC Canvas", "source": "mail", "kind": "task", "primary": "mail:x",
            "item": {"person": False, "source": "mail", "text": "", "replied": False, "received_ts": NOW - 3600},
            "due_ts": due_ts, "has_clock": has_clock,
            "due_label": guard.due_label(due_ts, NOW, TZ, has_clock=has_clock),
            "why": "Worksheet 3 is due soon.", "fingerprints": ["mail:x:1"], "account": "me", "quote": ""}


class NoModel:
    async def __call__(self, *args, **kwargs):
        raise AssertionError("prepare needs no model for a reminder")


def cards_for(items):
    state, cards = asyncio.run(prepare.prepare(items, {"calendar": []}, NoModel(), lambda: False, now=NOW, tz=TZ))
    assert state == "ok"
    return cards


def test_a_past_due_date_gives_no_reminder_card():
    past = datetime(2026, 10, 19, 12, 0, tzinfo=ZONE).timestamp()
    assert cards_for([org_item(past)]) == []
    plan_method, _ = prepare.choose(org_item(past), now=NOW, tz=TZ, linked=set(), handle=None)
    assert plan_method != "reminder"
    # Due an hour ago today: still no card.
    assert cards_for([org_item(NOW - 3600)]) == []


def test_a_future_due_date_gives_a_correctly_formatted_reminder_card():
    due = datetime(2026, 10, 27, 12, 0, tzinfo=ZONE).timestamp()       # Tue Oct 27, noon
    cards = cards_for([org_item(due)])
    assert len(cards) == 1
    card = cards[0]
    assert card["action"]["method"] == "reminder"
    remind_at = card["action"]["envelope"]["due_ts"]
    assert NOW < remind_at < due
    assert card["action"]["button"] == "Remind me Tue, Oct 27, 11:45 AM?"
    assert card["meta"] == "TUE · MAIL"
    assert "I can remind you Tue, Oct 27, 11:45 AM." in card["body"]


def test_a_card_body_never_claims_a_reminder_or_hold_was_already_set():
    due = datetime(2026, 10, 27, 12, 0, tzinfo=ZONE).timestamp()
    far = datetime(2026, 11, 9, 0, 0, tzinfo=ZONE).timestamp()
    cards = cards_for([org_item(due), org_item(far, has_clock=False, title="Finish HW3, Statistics")])
    assert cards
    for card in cards:
        assert "I've set up" not in card["body"] and "I've lined up" not in card["body"]
        assert card["action"]["button"] == card["action"]["button"].strip()
        assert "tue oct" not in card["action"]["button"]
    far_card = next(c for c in cards if c["title"].startswith("Finish HW3"))
    assert far_card["meta"] == "NOV 9 · MAIL"          # a far date is named, never a bare weekday


def test_the_card_cap_stays_five():
    assert prepare.MAX_CARDS == 5
    due = datetime(2026, 10, 27, 12, 0, tzinfo=ZONE).timestamp()
    many = [dict(org_item(due + n * 3600, title=f"Finish Worksheet {n}, Statistics"), primary=f"mail:{n}",
                 fingerprints=[f"mail:{n}:1"]) for n in range(8)]
    assert len(cards_for(many)) == 5


def test_a_due_date_change_replaces_the_date_an_item_was_created_with():
    row = rollup([("HW3 , Statistics", "Apr 29 at 11:59pm")])
    row["body"] = row["body"].replace(
        " <link>\n", " <link>\n--------------------------------\n\n Assignment Due Date Changed: HW3 , Statistics\n\n"
                     " due: Oct 26 at 11:59pm\n\n <link>\n", 1)
    entries = sources.digest_entries(row, now=NOW, tz=TZ)
    assert [(e["name"], e["due_ts"]) for e in entries] == [
        ("HW3, Statistics", datetime(2026, 10, 26, 23, 59, tzinfo=ZONE).timestamp())]
