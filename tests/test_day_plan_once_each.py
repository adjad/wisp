"""get_my_day_plan lists each item once, across every section. Synthetic rows only."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from service.proactive.prepare import todo_id
from service.tools.proactive_tools import render_day_plan, render_range_plan

TZ = "America/Los_Angeles"
ZONE = ZoneInfo(TZ)


def ts(day, hour=9, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE).timestamp()


NOW = ts(4)  # Wed Nov 4


def todo(title, *, id=None, source="reminders", due=None, label="", card=None, **extra):
    row = {"title": title, "source": source, "due_ts": due, "due_label": label, "done": False}
    if id:
        row["id"] = id
    if card:
        row["card_id"] = card
    return {**row, **extra}


def card(title, *, id="card:1", todo_ref=None, why="", **extra):
    row = {"id": id, "title": title, "why": why, "action": {"method": "read"}, "sources": []}
    if todo_ref:
        row["todo_id"] = todo_ref
    return {**row, **extra}


def snap(*, todos=(), cards=(), threads=()):
    return {"state": "ready", "summary": "", "todos": list(todos), "cards": list(cards), "threads": list(threads)}


def day(s, word="today"):
    return render_day_plan(s, now=NOW, tz=TZ, day=word)


def week(s):
    return render_range_plan(s, now=NOW, tz=TZ, start=ts(4, 0), end=ts(9, 0), label="this week")


def lines_with(text, needle):
    return [line for line in text.splitlines() if needle.casefold() in line.casefold()]


# --- suggested next steps vs other to-dos -------------------------------------------------

def test_a_card_and_its_todo_print_once_by_id_with_different_wording():
    text = day(snap(
        todos=[todo("Send Priya the W-9", id="todo:a", source="mail", label="Fri")],
        cards=[card("Reply to Priya about the W-9 form", todo_ref="todo:a", why="She asked by Friday.")]))
    assert "Other to-dos:" not in text
    assert lines_with(text, "W-9") == ["- Reply to Priya about the W-9 form · Fri — She asked by Friday."]


def test_the_todos_card_id_links_too():
    text = day(snap(todos=[todo("Pay rent", id="todo:a", card="card:9")],
                    cards=[card("Pay the November rent", id="card:9")]))
    assert lines_with(text, "rent") == ["- Pay the November rent"]


def test_equal_titles_match_without_ids_despite_case_and_punctuation():
    text = day(snap(todos=[todo("Buy printer paper!", label="Sat")],
                    cards=[card("buy  printer paper", why="You are nearly out.")]))
    assert len(lines_with(text, "printer paper")) == 1
    assert "· Sat — You are nearly out." in text


def test_titles_clipped_at_different_lengths_still_match():
    long = "Finish the quarterly planning document for the operations team and send it to the group"
    text = day(snap(todos=[todo(long + " before the review on Friday")], cards=[card(long[:84] + "…")]))
    assert len(lines_with(text, "quarterly planning")) == 1


def test_the_fuller_todo_title_survives_a_shorter_card():
    text = day(snap(todos=[todo("Call Dr. Patel about the test results", id="todo:a")],
                    cards=[card("Call Dr. Patel", todo_ref="todo:a")]))
    assert lines_with(text, "Patel") == ["- Call Dr. Patel about the test results"]


def test_different_items_with_similar_titles_both_stay():
    text = day(snap(todos=[todo("Reply to Dhruv")], cards=[card("Reply to Dhruv about the balcony")]))
    assert len(lines_with(text, "Dhruv")) == 2


def test_same_title_from_different_people_is_not_merged():
    text = day(snap(todos=[todo("RSVP", source="mail", who="Avika"), todo("RSVP", source="mail", who="Dhruv")]))
    assert len(lines_with(text, "RSVP")) == 2


def test_empty_titles_never_match():
    text = day(snap(todos=[todo("!!!"), todo("???")]))
    assert "!!!" in text and "???" in text


def test_the_snapshot_is_not_changed():
    s = snap(todos=[todo("Pay rent", id="todo:a", label="Fri")], cards=[card("Pay the rent now", todo_ref="todo:a")])
    before = repr(s)
    day(s)
    assert repr(s) == before


# --- time-bound and overdue ---------------------------------------------------------------

def test_a_time_bound_item_drops_its_card_and_keeps_the_reason():
    text = day(snap(
        todos=[todo("Submit the lab report", id="todo:a", source="mail", due=ts(4, 17), label="Today 5 PM")],
        cards=[card("Submit lab report", todo_ref="todo:a", why="Your TA needs it tonight.")]))
    assert "Suggested next steps:" not in text
    assert lines_with(text, "lab report") == ["- 5 pm · Submit the lab report (email) — Your TA needs it tonight."]


def test_a_deadline_card_that_only_restates_the_title_adds_nothing():
    text = day(snap(
        todos=[todo("Study for STAT 5", id="todo:a", due=ts(4, 18))],
        cards=[card("Study for STAT 5", todo_ref="todo:a", why="Study for STAT 5 is due today.",
                    action={"method": "block_time", "slot": {"start": ts(4, 14), "end": ts(4, 15)}})]))
    assert lines_with(text, "STAT 5") == ["- 6 pm · Study for STAT 5 (reminder)"]


def test_an_overdue_item_drops_its_card_and_its_undated_twin():
    text = day(snap(
        todos=[todo("Submit W-9", id="todo:a", source="mail", due=ts(1), label="Overdue · 3 days"),
               todo("submit w-9", source="notes")],
        cards=[card("Reply about the W-9", todo_ref="todo:a", why="Payroll asked twice.")]))
    assert "Other to-dos:" not in text and "Suggested next steps:" not in text
    assert lines_with(text, "W-9") == ["- Reply about the W-9 · Overdue · 3 days — Payroll asked twice."]


def test_a_calendar_event_and_a_date_only_mail_item_on_one_day_are_one_line():
    text = day(snap(todos=[todo("Diwali dinner", source="calendar", due=ts(4, 19), timed=True),
                           todo("Diwali dinner", source="mail", due=ts(4, 23, 59), label="Today")]))
    assert lines_with(text, "Diwali") == ["- 7 pm · Diwali dinner (calendar)"]


def test_one_title_at_two_times_on_one_day_stays_two_lines():
    text = day(snap(todos=[todo("Lab section", source="calendar", due=ts(4, 10)),
                           todo("Lab section", source="calendar", due=ts(4, 14))]))
    assert len(lines_with(text, "Lab section")) == 2


def test_one_title_overdue_and_due_today_stays_two_lines():
    text = day(snap(todos=[todo("Water plants", due=ts(2), label="Overdue · 2 days"), todo("Water plants", due=ts(4, 18))]))
    assert len(lines_with(text, "Water plants")) == 2


def test_a_card_due_on_another_day_is_not_the_same_titled_todo():
    text = day(snap(todos=[todo("Water plants", due=ts(4, 18))], cards=[card("Water plants", due_ts=ts(6, 18))]))
    assert len(lines_with(text, "Water plants")) == 2


# --- waiting threads ----------------------------------------------------------------------

def thread(item="mail:1", who="Avika", summary="Asked if you're free Friday night.", **extra):
    return {"id": "thread:" + item, "who": who, "source": "messages", "title": "Friday", "summary": summary,
            "needs_reply": True, **extra}


def test_a_thread_its_card_and_its_todo_print_once():
    text = day(snap(
        todos=[todo("Answer Avika about Friday", id=todo_id("mail:1"), source="messages", label="Fri")],
        cards=[card("Reply to Avika", todo_ref=todo_id("mail:1"), why="She needs to book tonight.",
                    sources=[{"kind": "messages", "ref": "mail:1"}])],
        threads=[thread()]))
    assert "Suggested next steps:" not in text and "Other to-dos:" not in text
    assert lines_with(text, "Avika") == ["- Avika (text): Asked if you're free Friday night. · Fri — She needs to book tonight."]


def test_a_time_bound_item_keeps_that_someone_is_waiting():
    text = day(snap(todos=[todo("Confirm dinner", id=todo_id("mail:1"), source="messages", due=ts(4, 17), label="Today 5 PM")],
                    threads=[thread()]))
    assert "Waiting on a reply from you:" not in text
    assert lines_with(text, "Confirm dinner") == [
        "- 5 pm · Confirm dinner (text) — Avika is waiting on a reply: Asked if you're free Friday night."]


def test_a_thread_never_matches_on_its_subject_line():
    text = day(snap(todos=[todo("Friday", source="calendar", due=ts(4, 19))], cards=[card("Friday")],
                    threads=[thread()]))
    assert "Waiting on a reply from you:" in text and "Avika (text)" in text


# --- the caps count what is left ----------------------------------------------------------

def test_more_lines_count_only_what_remains():
    rows = [todo(f"Task {i}", id=f"todo:{i}") for i in range(8)]
    cards = [card(f"Do task {i} properly", id=f"card:{i}", todo_ref=f"todo:{i}") for i in range(2)]
    text = day(snap(todos=rows, cards=cards))
    assert "…and 1 more" in text.split("Other to-dos:")[1]  # 6 left, 5 shown; 8 - 5 would say 3
    assert "…and" not in text.split("Other to-dos:")[0]
    assert "- Task 2" in text and "- Task 0" not in text and "- Do task 0 properly" in text


def test_no_more_line_when_only_repeats_passed_the_cap():
    rows = [todo(f"Task {i}", id=f"todo:{i}") for i in range(6)]
    text = day(snap(todos=rows, cards=[card("Do task 0 properly", todo_ref="todo:0")]))
    assert "…and" not in text


def test_a_range_more_line_counts_only_what_remains():
    rows = [todo(f"Task {i}", id=f"todo:{i}") for i in range(4)]
    text = week(snap(todos=rows, cards=[card("Do task 0 properly", todo_ref="todo:0")]))
    assert "…and" not in text and len(lines_with(text, "task 0")) == 1


# --- tomorrow and range plans -------------------------------------------------------------

def test_tomorrow_plan_prints_each_item_once():
    text = day(snap(
        todos=[todo("Submit CSE 20 lab", id="todo:a", due=ts(5, 23, 59)),
               todo("Buy printer paper"), todo("Essay draft", id="todo:b", source="mail", due=ts(7, 23, 59), label="Sat")],
        cards=[card("Submit lab", id="card:1", todo_ref="todo:a", why="Due tomorrow night."),
               card("Work on the essay draft", id="card:2", todo_ref="todo:b"),
               card("buy printer paper", id="card:3")]), "tomorrow")
    assert lines_with(text, "CSE 20") == ["- 11:59 pm · Submit CSE 20 lab (reminder) — Due tomorrow night."]
    assert lines_with(text, "essay") == ["- Work on the essay draft · Sat"]
    assert len(lines_with(text, "printer paper")) == 1


def test_range_plan_prints_each_item_once():
    text = week(snap(
        todos=[todo("HW4", id="todo:a", source="mail", due=ts(6, 17), label="Fri 5 PM"),
               todo("Submit W-9", id="todo:b", due=ts(2), label="Overdue · 2 days"),
               todo("Buy printer paper"), todo("Call Dr. Patel about results", id="todo:c")],
        cards=[card("Finish HW4", id="card:1", todo_ref="todo:a", why="Worth 10 percent."),
               card("File W-9", id="card:2", todo_ref="todo:b"),
               card("Buy printer paper", id="card:3", why="You are nearly out."),
               card("Call Dr. Patel", id="card:4", todo_ref="todo:c")]))
    assert lines_with(text, "HW4") == ["- 5 pm · Finish HW4 (email) — Worth 10 percent."]
    assert len(lines_with(text, "W-9")) == 1 and "Submit W-9 · Overdue · 2 days" in text
    assert lines_with(text, "printer paper") == ["- Buy printer paper — You are nearly out."]
    assert lines_with(text, "Patel") == ["- Call Dr. Patel about results"]


def test_range_plan_keeps_a_weekly_class_on_each_of_its_days():
    text = week(snap(todos=[todo("Attendance", source="calendar", due=ts(d, 16)) for d in (4, 5, 6)]))
    assert len(lines_with(text, "Attendance")) == 3


def test_range_plan_matches_a_thread_by_id_too():
    text = week(snap(todos=[todo("Answer Avika", id=todo_id("mail:1"), source="messages")], threads=[thread()]))
    assert "Other to-dos:" not in text and len(lines_with(text, "Avika")) == 1
