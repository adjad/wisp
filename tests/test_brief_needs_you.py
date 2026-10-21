"""The Brief's "Needs you today" line (daily_workspace.needs_you): synthetic
attention items only; no native sources or models."""
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-brief-needs-you-tests-")
os.environ["WISP_HOME"] = _scratch.name

from service.assistant.daily_workspace import NEEDS_YOU_SHOWN, assess, needs_you, not_a_need

ZONE = "America/Los_Angeles"


def at(hour, minute=0, day=14):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()


NOW, END = at(10), at(0, day=15)


def reminder(title, due, ident=None, **extra):
    return dict(id=ident or "reminders:" + title, source="reminders", title=title, due_ts=due,
                reason="Due in this day.", **extra)


def task(title, due=None):
    return dict(id="wisp:" + title, source="wisp", title=title, due_ts=due, reason="On your day plan.")


def mail(title, sender, person=True, received=None):
    return dict(id="email:" + title + sender, source="email", title=title, due_ts=None, sender=sender,
                person=person, received_ts=received if received is not None else at(9), reason="Review this source")


def text(sender, received, replied=False, body="Sender: hi"):
    return dict(id=f"messages:{sender}:{received}", source="messages", title=body, due_ts=None, sender=sender,
                person=True, received_ts=received, replied=replied, reason="Important conversation context")


def pick(attention):
    return needs_you(attention, now=NOW, day_end=END)


def texts(result):
    return [(i["kind"], i["text"]) for i in result["items"]]


def test_order_is_overdue_due_plan_then_people():
    result = pick([
        text("Priya", at(9, 30)),
        mail("Lab report feedback", "Dr. Patel"),
        task("Finish slides"),
        reminder("Submit PA 2", at(23, 59)),
        reminder("Pay rent", at(9, day=12)),
    ])
    assert texts(result) == [("overdue", "Pay rent"), ("due", "Submit PA 2"), ("plan", "Finish slides"),
                             ("mail", "“Lab report feedback” from Dr. Patel")]
    assert result["more"] == 1


def test_due_today_sorted_by_time_and_carries_due():
    result = pick([reminder("Late", at(18)), reminder("Early", at(12))])
    assert [i["text"] for i in result["items"]] == ["Early", "Late"]
    assert result["items"][0]["due_ts"] == at(12)


def test_undated_reminders_tomorrow_calendar_notes_and_machines_are_left_out():
    result = pick([
        dict(reminder("Someday", None), reason="No due date; choose whether to work on it today."),
        reminder("Tomorrow's thing", at(9, day=15)),
        dict(id="calendar:x", source="calendar", title="Standup", due_ts=at(11), reason="Scheduled commitment"),
        dict(id="notes:x", source="notes", title="buy milk", due_ts=None, reason="Task candidate"),
        mail("PA 2 has been posted", "Canvas", person=False),
    ])
    assert result == dict(items=[], more=0)


def test_withheld_subject_named_by_class_and_sender_never_placeholder():
    result = pick([mail("Deadline notice (private details omitted).", "Dr. Patel"),
                   mail("Private details omitted.", "Sam Lee"),
                   mail("Security incident notice (details omitted).", "Google")])
    shown = [i["text"] for i in result["items"]]
    assert "a deadline notice from Dr. Patel" in shown
    assert "an email from Sam Lee" in shown
    assert "a security notice from Google (unverified)" in shown
    assert not any("omitted" in s.lower() for s in shown)


def test_withheld_credential_mail_never_listed():
    result = pick([mail("Sign in to Canvas: Authentication details omitted.", "Canvas Support"),
                   reminder("Authentication details omitted.", at(12))])
    assert result["items"] == []


def test_texts_name_the_person_once_never_what_they_wrote():
    result = pick([text("Priya", at(8), body="Priya: are we still on for dinner"),
                   text("Priya", at(9), body="Priya: ??"),
                   text("Mom", at(9, 30)),
                   text("Sam", at(7), replied=True),
                   text("Me", at(9))])
    assert texts(result) == [("text", "a text from Mom"), ("text", "2 texts from Priya")]
    assert not any("dinner" in i["text"] for i in result["items"])


def test_junk_titles_and_task_twin_of_a_reminder_are_dropped():
    result = pick([reminder("nothing", at(12)), reminder("Pay rent", at(12)), task("Pay rent")])
    assert texts(result) == [("due", "Pay rent")]


def test_kept_review_task_is_not_listed_again_as_mail():
    result = pick([task("Review: Lab report feedback"), mail("Lab report feedback", "Dr. Patel"),
                   task("Review a deadline notice from Dr. Patel"),
                   mail("Deadline notice (private details omitted).", "Dr. Patel")])
    assert [i["kind"] for i in result["items"]] == ["plan", "plan"]


def test_shows_at_most_four_and_counts_the_rest():
    result = pick([reminder(f"Thing {n}", at(11 + n)) for n in range(7)])
    assert len(result["items"]) == NEEDS_YOU_SHOWN and result["more"] == 7 - NEEDS_YOU_SHOWN


def test_newsletter_never_reaches_attention_so_never_the_blurb():
    # Collection drops mass mail (mass_mail_filter); the blurb reads only what's left.
    row = dict(subject="Weekly digest: 12 new posts", sender="Campus News", sender_address="news@campus.edu")
    assert not_a_need(row)


def test_assess_attaches_the_same_selection_the_day_reads():
    day = "2026-10-14"
    state = dict(tasks=[dict(id="t1", title="Finish slides", status="active", day=day, priority=2, due_ts=None,
                             duration_minutes=30, pinned_start=None, kind="task", timezone=ZONE, revision=1)],
                 commitments=[], preferences=dict(start_minute=540, end_minute=1200, not_before=None),
                 sources={s: dict(available=True, last_sync=NOW, snapshot_started_at=NOW,
                                  diagnostics=dict(coverage_start=at(0), coverage_end=END))
                          for s in ("calendar", "reminders")})
    evidence = dict(reminders=dict(ready=True, detail="x", records=[dict(id="r1", title="Pay rent", due_ts=at(9, day=13),
                                                                          reference="reminders:r1")]))
    workspace = assess(day, ZONE, state, evidence, now=NOW)
    ids = {i["id"] for i in workspace["attention"]}
    assert texts(workspace["needs_you"]) == [("overdue", "Pay rent"), ("plan", "Finish slides")]
    assert all(i["id"] in ids for i in workspace["needs_you"]["items"])
