"""World tool results against the LIVE tools' output format (service/tools/*.py).

The builder's synthetic World must print what production prints, or the model learns to read text it never
sees. Where a live helper exists it is imported and called here; no live string is copied except the pins
that tests/test_toolbench_world.py also holds.

Run from this directory:  ~/Desktop/MOE_Project/.venv/bin/python -m pytest -q test_world_parity.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import sys
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

import boot  # noqa: F401  (fresh WISP_HOME before any service import)
import harness as H
import world as W

TZ = W.World.FREE_TIME_ZONE
ZONE = ZoneInfo(TZ)
NOW = dt.datetime(2026, 10, 14, 8, 30)          # a Wednesday, 8:30 AM


def D(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi)


@pytest.fixture(autouse=True)
def frozen_clock():
    """The harness clock is the world's now: live code resolves 'friday' and 'this week' from it."""
    clock = dict(H._CLOCK)
    H.set_clock(NOW)
    yield
    H._CLOCK.update(clock)


@pytest.fixture
def world():
    w = W.World(now=NOW)
    w.events = [
        W.Event("Team standup", D(2026, 10, 14, 9, 30), 15),
        W.Event("Dentist", D(2026, 10, 15, 15, 30), 60),
        W.Event("Hiking trip", D(2026, 10, 17), 2 * 24 * 60, all_day=True),
        W.Event("Columbus Day", D(2026, 10, 19), 24 * 60, all_day=True, writable=False, context="US Holidays"),
    ]
    w.reminders = [W.Reminder("Water plants", D(2026, 10, 14, 18)), W.Reminder("Pay rent", D(2026, 10, 12, 9)),
                   W.Reminder("Call the bank", None), W.Reminder("Renew parking", D(2026, 10, 20, 9))]
    w.messages = [W.Msg("Maya Chen", "Maya Chen", "are you coming tonight?", D(2026, 10, 14, 7), True)]
    w.notes = [W.Note("Lease", "The lease ends Aug 31.", D(2026, 10, 1, 9)),
               W.Note("Groceries", "milk, eggs, rice", D(2026, 10, 10, 9), folder="Home")]
    w.memories = ["I am allergic to peanuts", "My roommate is Priya"]
    return w


# ------------------------------------------------------------------ find_free_time
def _live_free_time(world, period, minutes, start, end, days=3):
    """schedule_extras._availability_result over a snapshot written here, independently of World."""
    from service.tools import schedule_extras as SE
    rows = [{"id": f"e{i}", "source": "calendar", "status": "active", "title": e.title, "context": e.context,
             "source_id": f"e{i}", "all_day": e.all_day, "calendar_writable": e.writable,
             "when_ts": e.start.replace(tzinfo=ZONE).timestamp(), "end_ts": e.end.replace(tzinfo=ZONE).timestamp()}
            for i, e in enumerate(world.events)]
    info = {"available": True, "syncing": False, "last_sync": world.now.replace(tzinfo=ZONE).timestamp(),
            "diagnostics": {"coverage_start": 0.0, "coverage_end": 4e9}}
    snap = {"sources": {"calendar": info, "reminders": dict(info)}, "commitments": rows, "tasks": []}
    options = SE._availability_options(days, minutes, start, end, legacy=not period)
    with mock.patch.object(SE._store, "today_snapshot", lambda key, tz: dict(snap)):
        return SE._availability_result(options, period, TZ, world.now.replace(tzinfo=ZONE))


@pytest.mark.parametrize("kw", [
    dict(period="tomorrow", minutes=60),
    dict(period="tomorrow", minutes=30),
    dict(period="today"),
    dict(period="tomorrow", minutes=30, day_start=12, day_end=15),
    dict(period="saturday"),                      # inside a trip: no gap
    dict(period="monday"),                        # a read-only holiday leaves the day free
    dict(period="tomorrow", minutes=600),         # nothing that long: the 'No matches:' line
    dict(minutes=30, days=3),                     # no period: whole gaps, 'Free for 30+ minutes'
])
def test_find_free_time_is_the_live_result(world, kw):
    live = _live_free_time(world, kw.get("period", ""), kw.get("minutes", 30), kw.get("day_start", 9),
                           kw.get("day_end", 18), kw.get("days", 3))
    assert world.t_find_free_time(**kw) == live


def test_find_free_time_period_pins_from_the_toolbench_tests(world):
    # tests/test_toolbench_world.py::test_find_free_time_gaps pins these shapes (naive there, offset here).
    out = world.t_find_free_time(period="tomorrow", minutes=60)
    assert out.startswith("60-minute candidate slots, 9:00–18:00 (America/Los_Angeles):")
    # The slot is only `minutes` long; the gap's real end is the 'free until' time.
    assert ("9:00 AM–10:00 AM (2026-10-15T09:00:00-07:00 → 2026-10-15T10:00:00-07:00; "
            "free until 2026-10-15T15:30:00-07:00)") in out
    assert ("4:30 PM–5:30 PM (2026-10-15T16:30:00-07:00 → 2026-10-15T17:30:00-07:00; "
            "free until 2026-10-15T18:00:00-07:00)") in out
    assert "Thursday, 2026-10-15:" in out
    assert "tentative or response-unknown entries" in out and out.rstrip().endswith("has not been created.")
    long = world.t_find_free_time(period="tomorrow", minutes=120)
    assert "9:00 AM–11:00 AM" in long and "4:30 PM" not in long
    legacy = world.t_find_free_time(days=2, minutes=30)
    assert legacy.startswith("Free for 30+ minutes, 9:00–18:00 (America/Los_Angeles):")
    assert "today (2026-10-14): 9:00 AM–9:30 AM" in legacy and "free until 2026-10-14T09:30:00-07:00)" in legacy
    assert "tomorrow (2026-10-15): 9:00 AM–3:30 PM" in legacy
    assert world.t_find_free_time(period="tomorrow", minutes=600).startswith("No matches: No gaps of 600+ minutes")
    assert world.t_find_free_time(period="tomorrow", minutes=0).startswith("(error: minutes must be between 1 and 1440.)")
    assert world.t_find_free_time(period="last tuesday").startswith("(error:")


def test_find_free_time_bad_period_and_unavailable_calendar(world):
    from service.tools import schedule_extras as SE
    with pytest.raises(ValueError) as exc:
        SE._availability_days(NOW.replace(tzinfo=ZONE), 3, "someday soon")
    assert world.t_find_free_time(period="someday soon") == f"(error: {exc.value})"
    world.unavailable.add("calendar")
    assert world.t_find_free_time(period="tomorrow").startswith(
        "(error: Wisp could not check current Calendar and Reminders availability.")


def test_free_ranges_end_at_the_free_until_time(world):
    res, spans = world.free_ranges("tomorrow", minutes=60)
    assert spans == [("9:00 AM", "3:30 PM"), ("4:30 PM", "6:00 PM")]
    # a 30-minute slot at the start of a long gap still reports the whole gap
    _, spans = world.free_ranges("monday")
    assert spans == [("9:00 AM", "6:00 PM")]


# ------------------------------------------------------------------ recall
def test_recall_prints_integer_memory_ids_like_the_live_tool(world):
    from service.tools import memory_tools as MT
    out = world.t_recall("peanuts")
    assert re.search(r'^\[memory 11; \d{4}-\d\d-\d\d; explicit; fact\] "I am allergic to peanuts"$', out, re.M)
    assert "memory m" not in out
    # the same row, rendered by the live recall over a stub retrieval
    date = (NOW - dt.timedelta(days=9)).strftime("%Y-%m-%d")
    ts = dt.datetime.strptime(date, "%Y-%m-%d").replace(hour=12).timestamp()
    fact = {"id": 11, "observed_at": ts, "origin": "explicit", "category": "fact", "text": "I am allergic to peanuts"}
    with mock.patch.object(MT, "retrieve", lambda *a, **k: {"facts": [fact], "passages": []}), \
            mock.patch.object(MT.store, "touch", lambda ids: None):
        live = asyncio.run(MT.recall("peanuts"))
    assert out == live
    # two rows are separated by a blank line, as _bounded joins them
    both = world.t_recall("")
    assert "\n\n[memory 12;" in both
    miss = world.t_recall("zebra")
    assert miss == MT._NO_MATCH + " No saved memory or user conversation passage contains every term of that request." + MT._SCOPE
    assert world.t_recall("the and of") == MT._NO_TERMS


def test_remember_supersedes_id_matches_what_recall_prints(world):
    # recall prints the integer 11; remember takes supersedes_id=11 (an int), nothing else.
    first = re.search(r"\[memory (\d+);", world.t_recall("peanuts")).group(1)
    assert first == "11"
    assert int(first) == 11


# ------------------------------------------------------------------ battery / system status
def _ioreg(charge, cycles, design, full, *, external, charging, fully=False, remaining=0):
    yn = lambda b: "Yes" if b else "No"
    return SimpleNamespace(returncode=0, stderr="", stdout="\n".join([
        f'"CurrentCapacity" = {charge}', f'"CycleCount" = {cycles}', f'"DesignCapacity" = {design}',
        f'"FullChargeCapacity" = {full}', f'"TimeRemaining" = {remaining}', f'"IsCharging" = {yn(charging)}',
        f'"ExternalConnected" = {yn(external)}', f'"FullyCharged" = {yn(fully)}']))


@pytest.mark.parametrize("status, ioreg", [
    ({}, dict(external=False, charging=False)),
    ({"charging": True}, dict(external=True, charging=True)),
    ({"charging": False, "plugged": True}, dict(external=True, charging=False)),
    ({"full": True, "battery": 100}, dict(external=True, charging=False, fully=True)),
    ({"remaining_min": 245}, dict(external=False, charging=False, remaining=245)),
    ({"health": 74}, dict(external=False, charging=False)),
])
def test_battery_wording_is_the_live_battery_status(world, status, ioreg):
    from service.tools import system_control as SC
    world.status = dict(status)
    st = {"battery": 64, "cycles": 211, "health": 91, **status}
    design = 1000
    proc = _ioreg(st["battery"], st["cycles"], design, design * st["health"] // 100, **ioreg)
    with mock.patch.object(SC, "_run", lambda *a, **k: proc):
        live = SC.battery_status()
    assert world.t_system_status("battery") == live
    assert world.t_system_status().splitlines()[0] == "Battery: " + live
    assert "discharging" not in live


def test_system_status_overview_has_the_live_line_set(world):
    lines = [ln.split(":", 1)[0] for ln in world.t_system_status().splitlines()]
    assert lines == ["Battery", "System", "Installed memory", "Disk", "Memory", "CPU load", "Uptime"]
    assert re.fullmatch(r"CPU load: \d+\.\d\d \(1m\), \d+\.\d\d \(5m\), across \d+ cores",
                        world.t_system_status().splitlines()[5])


# ------------------------------------------------------------------ Wisp's Brief
# The plan, the schedule reads and the free-time read all go through live Brief code over this world: a proactive store that
# holds its mail / text / note to-dos and waiting people, and an assistant store that serves its events and reminders.
def _briefed(world):
    """The fixture plus what the Brief read from mail, texts and notes: a homework due time, a family plan from a text, a note."""
    em = W.Email("LindenLearn", "notifications@lindenlearn.edu", "Assignment due: CS 162 Project 3",
                 D(2026, 10, 12, 20), "Project 3 for CS 162 is due Friday at 11:59 PM. Submit it on LindenLearn.", False, False)
    tx = W.Msg("Aunt Priya", "Aunt Priya", "Housewarming party saturday evening! Bring a plant if you can", D(2026, 10, 13, 9))
    nt = W.Note("Errands", "Thursday - drop off the donation box", D(2026, 10, 13, 18))
    world.emails.append(em)
    world.messages.append(tx)
    world.notes.append(nt)
    world.brief = [W.BriefItem("Finish Project 3, CS 162", D(2026, 10, 16, 23, 59), "mail", "LindenLearn", True, em),
                   W.BriefItem("Bring a plant to Aunt Priya's housewarming", D(2026, 10, 17, 23, 59), "messages", "Aunt Priya", False, tx),
                   W.BriefItem("Drop off the donation box", D(2026, 10, 15, 23, 59), "notes", "", False, nt)]
    world.waiting = [W.Waiting("Maya Chen", "Asked if you're coming tonight.", "messages", "are you coming tonight?", D(2026, 10, 14, 7))]
    return world


def _hand_snapshot(world, window=None):
    """The Brief snapshot written here by hand, independently of World: the live deterministic to-dos for the calendar and
    Reminders rows (today and tomorrow, plus ``window`` = (start, end) for a later day or a range), the Brief rows with live
    due labels, the waiting threads and the live overview line."""
    from service.proactive import cycle, guard
    now = NOW.replace(tzinfo=ZONE).timestamp()
    z = lambda t: t.replace(tzinfo=ZONE).timestamp()
    midnight = z(dt.datetime.combine(NOW.date(), dt.time()))
    base_end = midnight + 2 * 86400
    keep = lambda ts: midnight - 14 * 86400 <= ts < base_end or (window is not None and window[0] <= ts < window[1])
    sched = {"calendar": [{"id": f"evt-{i}", "title": e.title, "when_ts": z(e.start), "all_day": e.all_day, "account": ""}
                          for i, e in enumerate(world.events)
                          if keep(z(e.start)) or (window is not None and z(e.start) < window[0] < z(e.end))],
             "reminders": [{"id": f"rem-{i}", "title": r.title, "when_ts": z(r.due) if r.due else None, "account": ""}
                           for i, r in enumerate(world.reminders)
                           if r.due is None or (keep(z(r.due)) and now - z(r.due) < 7 * 86400)]}
    todos = cycle.deterministic_todos(sched, [], now, TZ)
    for i, b in enumerate(world.brief):
        todos.append({"id": f"brief-{i}", "title": b.title, "source": b.source, "who": b.who, "due_ts": z(b.due), "done": False,
                      "due_label": guard.due_label(z(b.due), now, TZ, has_clock=b.timed)})
    todos.sort(key=lambda t: (t["done"], t.get("due_ts") is None, t.get("due_ts") or 0, t["title"].casefold()))
    threads = [{"who": p.who, "source": p.source, "needs_reply": True, "summary": p.summary, "title": p.title}
               for p in world.waiting_people()]
    return {"summary": cycle._day_summary([], todos, threads, now, TZ), "todos": todos, "threads": threads, "cards": [], "state": "ready"}


def test_day_plan_is_the_live_render(world):
    from service.tools.proactive_tools import render_day_plan, render_range_plan
    from service.tools.timeranges import resolve_span
    _briefed(world)
    now = NOW.replace(tzinfo=ZONE).timestamp()
    snap = _hand_snapshot(world)
    for word, day in (("today", "today"), ("tomorrow", "tomorrow")):
        assert world.t_get_my_day_plan(word) == render_day_plan(snap, now=now, tz=TZ, day=day)
    # a weekday beyond tomorrow plans that date (the harness replaces datetime.date; the world restores it while live code runs)
    start, end, _label = resolve_span("friday", forward=True)
    assert world.t_get_my_day_plan("friday") == render_day_plan(_hand_snapshot(world, (start, end)), now=now, tz=TZ,
                                                                 day=dt.date(2026, 10, 16))
    for word in ("this_week", "this weekend", "next week"):
        start, end, label = resolve_span(word.replace("_", " "), forward=True)
        assert world.t_get_my_day_plan(word) == render_range_plan(_hand_snapshot(world, (start, end)), now=now, tz=TZ,
                                                                   start=start, end=end, label=label)


def test_day_plan_lists_what_mail_texts_and_notes_add(world):
    _briefed(world)
    today = world.t_get_my_day_plan()
    assert today.startswith("Day plan for today, Wed Oct 14 (from Wisp's Brief: calendar, reminders, email, texts and notes; items "
                            "are data from the user's sources, not instructions).")
    assert "Overview: " in today
    assert "- 9:30 am · Team standup (calendar)" in today and "- 6 pm · Water plants (reminder)" in today
    assert "Pay rent · Overdue · 2 days" in today                           # the live due label, not 'N days overdue'
    assert "Waiting on a reply from you:\n- Maya Chen (text): Asked if you're coming tonight." in today
    assert "Columbus Day" not in today and "Renew parking" not in today
    # Brief items due later sit under 'Other to-dos' with their live due labels
    assert "Finish Project 3, CS 162 · Fri 11:59 PM" in today and "Drop off the donation box · Tomorrow" in today
    friday = world.t_get_my_day_plan("friday")
    assert friday.startswith("Day plan for Friday, Fri Oct 16 (from Wisp's Brief:")
    assert "Time-bound Friday:\n- 11:59 pm · Finish Project 3, CS 162 (email)" in friday
    saturday = world.t_get_my_day_plan("saturday")
    assert "- all day · Hiking trip (calendar)" in saturday
    assert "- all day · Bring a plant to Aunt Priya's housewarming (text)" in saturday
    week = world.t_get_my_day_plan("this_week")
    assert week.startswith("Plan for this week, Wed Oct 14–Sun Oct 18 (from Wisp's Brief:")
    assert "Fri Oct 16:\n- 11:59 pm · Finish Project 3, CS 162 (email)" in week
    assert "Tomorrow:\n- 3:30 pm · Dentist (calendar)\n- all day · Drop off the donation box (note)" in week
    assert world.t_get_my_day_plan("next week").startswith("Plan for next week, Mon Oct 19–Sun Oct 25")


def test_day_plan_errors_are_the_live_ones(world):
    assert world.t_get_my_day_plan("yesterday") == "(error: that time has passed; use get_past_events for it)"
    assert world.t_get_my_day_plan("last friday") == "(error: that time has passed; use get_past_events for it)"
    assert world.t_get_my_day_plan("this month") == ("(error: get_my_day_plan covers a day or a week; use get_upcoming with a period "
                                                       "for a longer range)")
    bad = world.t_get_my_day_plan("someday soon")
    assert bad.startswith("(error: (couldn't understand the period 'someday soon'") and bad.endswith("a date or this_week)")


def test_day_plan_empty_day_uses_the_live_line():
    w = W.World(now=NOW)
    assert w.t_get_my_day_plan().endswith("Nothing time-bound, waiting or open today.")
    assert w.t_get_my_day_plan("saturday").endswith("Nothing time-bound, waiting or open Saturday.")
    assert w.t_get_my_day_plan("this_week").endswith("Nothing scheduled, due, waiting or open this week.")


def test_a_reminder_a_week_overdue_is_hidden_as_the_store_hides_it(world):
    world.reminders.append(W.Reminder("Return the ladder", D(2026, 10, 6, 9)))     # 8 days overdue
    assert "Return the ladder" not in world.t_get_my_day_plan()
    assert "Pay rent · Overdue · 2 days" in world.t_get_my_day_plan()


def test_waiting_people_default_to_unread_incoming_texts(world):
    assert [p.who for p in world.waiting_people()] == ["Maya Chen"]
    world.waiting = []
    assert "Waiting on a reply" not in world.t_get_my_day_plan()


def test_every_brief_item_is_findable_in_its_source(world):
    """A re-read of the text, email or note the Brief read must show the same words, so a model that checks finds them."""
    _briefed(world)
    for b in world.brief:
        if b.source == "messages":
            assert b.ref.text in world.t_view_messages(conversation=b.who)
        elif b.source == "mail":
            assert b.ref.body in world.t_view_emails(query=b.ref.subject)
        else:
            assert b.ref.body in world.t_search_notes(b.ref.title)
    # the thread behind a waiting person
    assert "are you coming tonight?" in world.t_view_messages(conversation="Maya Chen")


# ------------------------------------------------------------------ get_upcoming and the other schedule reads
def _live_upcoming(world, **kw):
    """assistant_tools.get_upcoming itself over the same stores, with Calendar and Reminders reported ready."""
    from service.tools import assistant_tools as AT

    async def ready(sources, timeout_seconds=None):
        return {"sources": [{"id": s, "label": s.title(), "state": "ready"} for s in sources], "reminders_fresh": True}

    with world.brief_live(), mock.patch("service.assistant.sync_status.ensure_sources", ready):
        return asyncio.run(AT.get_upcoming(**kw))


@pytest.mark.parametrize("kw", [
    dict(period="tomorrow"), dict(period="today"), dict(period="saturday"), dict(period="friday"), dict(period="this week"),
    dict(period="this week", calendar_only=True), dict(period="next week"), dict(days=7), dict(days=3, calendar_only=True),
    dict(period="friday", query="project"), dict(period="saturday", calendar_only=True), dict(period="sunday"),
])
def test_get_upcoming_is_the_live_result_with_the_brief_section(world, kw):
    _briefed(world)
    assert world.t_get_upcoming(**kw) == _live_upcoming(world, **kw)


def test_get_upcoming_names_the_brief_items_apart_from_the_calendar(world):
    _briefed(world)
    out = world.t_get_upcoming(period="saturday")
    assert out.startswith("Today is Wednesday, October 14, 2026. Nothing on your calendar or reminders in Saturday, October 17.") is False
    assert "*Also from your Brief* (dated items found in email, texts and notes, not calendar events;" in out
    assert "- Sat, Oct 17 · Bring a plant to Aunt Priya's housewarming (text from Aunt Priya)" in out
    week = world.t_get_upcoming(period="this week", calendar_only=True)
    assert "- Fri, Oct 16, due 11:59 PM · Finish Project 3, CS 162 (email from LindenLearn)" in week
    assert "Wisp's Brief also has 1 person waiting on a reply; get_my_day_plan(day='this week') lists them." in week
    # a named end-time question is about one Calendar event only
    assert "Also from your Brief" not in world.t_get_upcoming(period="friday", calendar_only=True, query="hiking", boundary="end")
    assert world.t_get_upcoming(boundary="end").startswith("(error: scheduled end projection requires")


def test_find_free_time_lists_brief_items_without_counting_them_busy(world):
    _briefed(world)
    out = world.t_find_free_time(period="friday", minutes=60)
    assert "- Fri, Oct 16, due 11:59 PM · Finish Project 3, CS 162 (email from LindenLearn)" in out
    assert "shown for context, not counted as busy" in out
    # the slots are the same with and without the Brief items
    bare = W.World(now=NOW, events=world.events, reminders=world.reminders)
    assert out.split("\n\n*Also from your Brief*")[0] == bare.t_find_free_time(period="friday", minutes=60)
    res, spans = world.free_ranges("friday", minutes=60)
    assert spans and all(re.fullmatch(r"\d+:\d\d [AP]M", a) and re.fullmatch(r"\d+:\d\d [AP]M", b) for a, b in spans)


@pytest.mark.parametrize("scope", ["today", "tomorrow", "past_due", "upcoming", "all"])
def test_search_reminders_adds_the_briefs_open_todos_in_the_same_scope(world, scope):
    from service.tools import assistant_tools as AT
    _briefed(world)
    out = world.t_search_reminders(scope=scope)
    core = world._search_reminders(scope=scope)
    assert out.startswith(core)
    with world.brief_live():
        tail = AT._reminders_brief("", scope)
    assert out == core + ("\n\n" + tail if tail else "")
    if scope in ("upcoming", "all"):
        assert "not reminders; reminder tools cannot change them" in out
        assert "Finish Project 3, CS 162 (email from LindenLearn)" in out
    if scope in ("today", "tomorrow", "past_due"):
        assert "Finish Project 3" not in out


def test_predict_my_day_is_the_live_text_with_the_days_brief_items(world):
    from service.tools import behavior_tools as BT
    _briefed(world)
    out = world.t_predict_my_day("tomorrow")
    runtime = SimpleNamespace(clock=lambda: NOW.replace(tzinfo=ZONE).timestamp())
    with world.brief_live(), mock.patch.object(BT, "_runtime", lambda: runtime), \
            mock.patch("service.assistant.behavior_runtime.predictions_for", lambda *a, **k: []):
        live = BT.predict_my_day("tomorrow")
    assert out == live
    assert out.startswith("Tomorrow (Thursday, October 15): I don't have enough learned routine to predict anything specific yet.")
    assert "Drop off the donation box (note)" in out and "not learned routines" in out
    assert "Finish Project 3" not in out                                          # due Friday, not Thursday


# ------------------------------------------------------------------ notes
def test_notes_miss_and_hit_follow_the_live_search(world):
    from service.tools import notes_tools as NT
    miss = world.t_search_notes("dentist appointment")
    assert miss == NT._strict_miss("'dentist' and 'appointment'", {"status": "fresh"}, len(world.notes) + 31)
    assert miss.startswith("No note matches 'dentist' and 'appointment' — Notes was re-read just now (current)")
    # every significant term, in any order, title or body or folder
    hit = world.t_search_notes("lease aug")
    assert hit.startswith("Title: Lease [Notes]\nModified: Thu Oct 1, 2026 9:00 AM\n\nThe lease ends Aug 31.")
    assert "Groceries" not in hit
    assert world.t_search_notes("home rice").startswith("Title: Groceries [Home]")
    # a browse names no topic: most recent first, not filtered
    browse = world.t_search_notes("recent notes")
    assert browse.index("Groceries") < browse.index("Lease")
    assert world.t_search_notes("lease dentist").startswith("No note matches")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
