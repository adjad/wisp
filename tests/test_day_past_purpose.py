"""A reminder to get ready for a dated event stops asking for time once that date passes.

From the user's Day on 2026-11-04: "Review all of this before your 10/30 midterm." still
read Overdue with Plan time five days after the midterm. It now goes to the Old reminders
Keep/Delete question early, and nothing changes in Reminders without a click. Late work
and anything still ahead stay. Synthetic titles and a temporary database only.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-day-past-purpose-tests-")
os.environ["WISP_HOME"] = _scratch.name

from service.assistant.stale_reminders import (ASK_AFTER_S, DAY_S, classify, classify_overdue,  # noqa: E402
                                               passed_date)
from service.assistant.store import AssistantStore, parse_undated_snapshot  # noqa: E402


def local(year, month, day, hour=12):
    return datetime(year, month, day, hour).timestamp()


NOW = local(2026, 11, 4, 16)
DUE = local(2026, 10, 30, 9)
MIDTERM = "Review all of this before your 10/30 midterm."


@pytest.mark.parametrize("title,label", [
    (MIDTERM, "10/30"),
    ("Study notes for the Oct 30 midterm", "Oct 30"),
    ("Review for STAT-5 exam on October 30th", "October 30th"),
    ("Get ready for the final, 10/30", "10/30"),
    ("Attend 10/30 review session", "10/30"),
    ("10/30 midterm", "10/30"),
    ("Midterm on Oct 30", "Oct 30"),
    ("Before my 10/30/26 interview, print resume", "10/30/26"),
    ("Pack for trip 2026-10-30", "2026-10-30"),
    # Any event after a calendar date, not a fixed list: future titles won't be midterms.
    ("Update resume before the 10/30 career fair", "10/30"),
    ("Practice lines before the school play on Oct 30", "Oct 30"),
    ("Buy a gift for Sam's 10/30 housewarming", "10/30"),
    ("Reminder: Midterm on 10/30", "10/30"),
])
def test_getting_ready_for_a_passed_event_is_recognised(title, label):
    found = passed_date(title, DUE, NOW)
    assert found is not None and found.label == label
    assert found.on == date(2026, 10, 30)


@pytest.mark.parametrize("title", [
    "Finish HW4 by Friday",                         # no calendar date at all
    "Finish HW4 by 10/30",                          # late work is still work
    "Pay rent before 10/30",                        # a date, but no event that passed
    "Submit paper for the 10/30 deadline",          # a deadline is not an event
    "Review 10/30 midterm results",                 # what comes after the event
    "Review before 10/30 midterm grade appeal",
    "Ask about makeup for the 10/30 midterm",
    "Reschedule missed 10/30 exam",
    "Email the prof before the 10/30 quiz",
    "Finish this by Friday",
    "Pay rent before Friday",
    "Clean up before Friday night",                 # a weekday needs a known event
    "Finish before 10/30 at noon",                  # says when, names no event
    "Get this done before end of day on 10/30",
    "Finish essay before the 10/30 lecture",        # late work can still be handed in
    "Turn in lab before 10/30 session",
    "Review all of this before your midterm",
    "Review before your 11/10 midterm",             # still ahead
    "Review before your 11/4 midterm",              # today still counts
    "Review before your 10/30 or 11/10 midterm",    # the second date hasn't come
    "Call about 10/30 midterm",
    "Review 30/10 midterm",                         # not a month/day
    "10/30 midterm study group notes",              # more than the event itself
    "",
])
def test_anything_possibly_actionable_stays(title):
    assert passed_date(title, DUE, NOW) is None


@pytest.mark.parametrize("title,label", [
    ("Review before Friday's midterm", "Friday"),      # due Fri Oct 30
    ("Prep for the interview tomorrow", "Tomorrow"),
    ("Attend the midterm review session this Monday", "This Monday"),
    ("Friday midterm", "Friday"),
])
def test_a_weekday_counts_from_the_reminders_own_time(title, label):
    found = passed_date(title, DUE, NOW)
    assert found is not None and found.label == label


def test_a_weekday_is_not_over_while_any_reading_is_ahead():
    # Due Fri Oct 30: "next Monday" could be Nov 2 or Nov 9.
    assert passed_date("Attend the session next Monday", DUE, local(2026, 11, 1)) is None
    assert passed_date("Attend the session next Monday", DUE, local(2026, 11, 9)) is None
    assert passed_date("Attend the session next Monday", DUE, local(2026, 11, 10)) is not None
    # This Friday's own day still counts as ahead.
    assert passed_date("Review before Friday's midterm", DUE, DUE) is None
    # With no time of its own (mail, notes) a weekday means nothing yet.
    assert passed_date("Review before Friday's midterm", NOW, NOW) is None
    assert passed_date("Review before your 10/30 midterm", NOW, NOW) is not None


def test_a_yearless_date_near_new_year_means_the_nearest_one():
    # Written for January, due in December: 1/5 is next month, not last January.
    assert passed_date("Review before your 1/5 midterm", local(2026, 12, 28), local(2026, 12, 29)) is None
    # The same reminder once January 5 has really gone by.
    later = passed_date("Review before your 1/5 midterm", local(2026, 12, 28), local(2027, 1, 7))
    assert later is not None and later.on == date(2027, 1, 5)
    # December's date read in January is last month's.
    back = passed_date("Review before your 12/28 midterm", local(2026, 12, 27), local(2027, 1, 3))
    assert back is not None and back.on == date(2026, 12, 28)


def test_a_yearless_date_far_from_the_reminder_is_not_guessed_at():
    # Due Nov 2 and naming 5/1: it could be last May or next May.
    assert passed_date("Review before your 5/1 midterm", local(2026, 11, 2), NOW) is None
    # A stated year needs no guess.
    assert passed_date("Review before your 5/1/26 midterm", local(2026, 11, 2), NOW).on == date(2026, 5, 1)
    assert passed_date("Review before your 2/29 midterm", local(2026, 2, 20), NOW) is None


def test_policy_states():
    assert classify_overdue(DUE, None, NOW, MIDTERM) == "ask"
    assert classify_overdue(DUE, "keep", NOW, MIDTERM) == "kept"
    assert classify_overdue(DUE, None, NOW, "Finish HW4 by Friday") == "fresh"
    # Set to ring later: not overdue, so the user's choice stands.
    assert classify_overdue(NOW + 3600, None, NOW, MIDTERM) == "fresh"
    # Past the question's window it goes quiet, as every other old reminder does.
    assert classify_overdue(NOW - 15 * DAY_S, None, NOW, "Review before your 10/15 midterm") == "stale"
    # Undated: the same, aged from when it was written; an unknown age is never hidden.
    assert classify(DUE - DAY_S, None, NOW, MIDTERM) == "ask"
    assert classify(None, None, NOW, MIDTERM) == "unknown"
    assert classify(DUE - DAY_S, "keep", NOW, MIDTERM) == "kept"
    # No title (an older caller) reads as it always did.
    assert classify_overdue(DUE, None, NOW) == "fresh"


# --- other sources: no Keep/Delete there, they just stop asking for time ------------------

def _assess(tasks=(), **records):
    from service.assistant.daily_workspace import assess
    from service.assistant.today import day_bounds
    lo, hi = day_bounds("2026-11-04", "America/Los_Angeles")
    state = dict(tasks=list(tasks), commitments=[], preferences=dict(start_minute=540, end_minute=1200, not_before=None),
                 sources={s: dict(available=True, last_sync=NOW, snapshot_started_at=NOW,
                                  diagnostics=dict(coverage_start=lo, coverage_end=hi)) for s in ("calendar", "reminders")})
    return assess("2026-11-04", "America/Los_Angeles", state, records, now=NOW)


def _mail(identity, subject):
    return dict(id=identity, title=subject, quote=subject, sender="Pat Doe", person=True,
                review_reason="Review this source for requests or changes.")


def test_mail_about_a_passed_event_gets_no_reading_block_but_stays_in_attention():
    result = _assess(email=dict(ready=True, records=[_mail("a", "Midterm on 10/30"), _mail("b", "Midterm on 11/10"),
                                                     _mail("c", "Your 10/30 midterm results")]))
    block = [b for b in result["proposed_plan"]["blocks"] if b["kind"] == "suggestion"]
    assert len(block) == 1 and {m["title"] for m in block[0]["members"]} == {"Midterm on 11/10", "Your 10/30 midterm results"}
    assert {i["title"] for i in result["attention"]} >= {"Midterm on 10/30", "Midterm on 11/10"}


def test_todos_from_mail_and_notes_for_a_passed_event_stop_offering_time():
    from service.proactive.cycle import without_passed_events
    todo = lambda title, source, **more: dict(title=title, source=source, **more)
    kept = without_passed_events({
        "mail": todo("Study for the 10/30 quiz", "mail"),
        "note": todo("Bring snacks for the Oct 30 potluck", "notes"),
        "late": todo("Finish HW4 by 10/30", "mail"),
        "ahead": todo("Study for the 11/10 quiz", "mail"),
        "kept": todo(MIDTERM, "reminders", due_ts=DUE),     # Kept by the user, or it wouldn't be here
        "event": todo("10/30 midterm", "calendar"),
    }, NOW)
    assert set(kept) == {"late", "ahead", "kept", "event"}


def _manual(store, title):
    return store.add_manual(title, DUE)["id"]


def test_day_evidence_drops_a_wisp_only_reminder_for_a_passed_event(store):
    from service.assistant.daily_workspace import collect_evidence
    ids = [_manual(store, "Study for the 10/30 quiz"), _manual(store, "Finish HW4 by Friday")]
    state = store.today_snapshot("2026-11-04", "America/Los_Angeles")
    titles = {r["title"] for r in collect_evidence(store, state, "2026-11-04", "America/Los_Angeles", now=NOW)["reminders"]["records"]}
    assert titles == {"Finish HW4 by Friday"}
    # Display only: the row itself is untouched.
    assert store._db.execute("SELECT status FROM scheduled_commitments WHERE id=?", (ids[0],)).fetchone()["status"] == "active"


# --- through the store ------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    s = AssistantStore(tmp_path / "assistant.db")
    yield s
    s._db.close()


def _sync(store, events=(), undated=()):
    store.sync_source("reminders", list(events),
                      diagnostics={"authorized": True, "count": len(events), "snapshot_started_at": 1000.0},
                      undated=parse_undated_snapshot(list(undated), len(undated)))


def _reminder(sid, title, when):
    return {"source_id": sid, "kind": "reminder", "title": title, "when_ts": when}


def _world(store):
    _sync(store, events=[_reminder("mid", MIDTERM, DUE), _reminder("hw", "Finish HW4 by Friday", DUE),
                         _reminder("ahead", "Review before your 11/10 midterm", DUE)],
          undated=[{"source_id": "u1", "title": "Study for the 10/30 quiz", "context": "School",
                    "created_ts": DUE - DAY_S},
                   {"source_id": "u2", "title": "Buy stamps", "context": "", "created_ts": DUE - DAY_S}])


def test_it_becomes_an_old_reminder_question_and_nothing_else_changes(store):
    _world(store)
    asks = {a["source_id"]: a for a in store.stale_reminder_asks(NOW)}
    assert set(asks) == {"mid", "u1"}
    assert asks["mid"]["passed_date"] == "10/30" and asks["mid"]["reason"] == "overdue"
    assert asks["mid"]["due_ts"] == DUE and asks["mid"]["actions"] == ["keep", "delete"]
    assert asks["u1"]["passed_date"] == "10/30" and asks["u1"]["reason"] == "undated"
    # Still on record, active: Wisp changed nothing, it only stopped offering it.
    assert store.stale_reminder_target("mid", NOW)["stale_state"] == "ask"
    rows = store._db.execute("SELECT status FROM scheduled_commitments WHERE source_id='mid'").fetchall()
    assert [r["status"] for r in rows] == ["active"]


def test_readers_skip_it_but_keep_what_is_still_actionable(store):
    _world(store)
    hidden = store.hidden_commitment_ids(NOW)
    ids = {r["source_id"]: r["id"] for r in store._db.execute(
        "SELECT id,source_id FROM scheduled_commitments").fetchall()}
    assert ids["mid"] in hidden and ids["hw"] not in hidden and ids["ahead"] not in hidden
    titles = {r["title"] for r in store.hidden_reminders(NOW)}
    assert titles == {MIDTERM, "Study for the 10/30 quiz"}
    # The read the agenda's Overdue row and Still to fit are built from.
    shown = {r["title"] for r in store.history(days=60, now=NOW)}
    assert MIDTERM not in shown and {"Finish HW4 by Friday", "Review before your 11/10 midterm"} <= shown


def test_keep_brings_it_back_for_good(store):
    _world(store)
    store.keep_stale_reminder("mid", MIDTERM)
    assert "mid" not in {a["source_id"] for a in store.stale_reminder_asks(NOW)}
    assert store.stale_reminder_target("mid", NOW)["stale_state"] == "kept"
    ids = {r["source_id"]: r["id"] for r in store._db.execute(
        "SELECT id,source_id FROM scheduled_commitments").fetchall()}
    assert ids["mid"] not in store.hidden_commitment_ids(NOW)


def test_a_day_task_planned_from_it_hides_with_it_unless_planned_after(store):
    _world(store)
    since = next(r["hidden_since"] for r in store.hidden_reminders(NOW) if r["title"] == MIDTERM)
    assert since == local(2026, 10, 31, 0) and since < DUE + ASK_AFTER_S
    base = dict(status="active", revision=1, day="2026-11-04", timezone="America/Los_Angeles", kind="task",
                duration_minutes=30, priority=2, due_ts=None, pinned_start=None, title=MIDTERM)
    planned_from_it = dict(base, id="t1", origin_ts=DUE)      # what Plan time recorded
    chosen_since = dict(base, id="t2", origin_ts=NOW - 3600)  # the user's own, after it passed
    other = dict(base, id="t3", title="Finish HW4 by Friday", origin_ts=DUE)
    shown = store.hide_stale_tasks([planned_from_it, chosen_since, other], NOW)
    assert [t["id"] for t in shown] == ["t2", "t3"]
