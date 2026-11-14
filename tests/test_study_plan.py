"""service/study/plan.py and service/study/day_hook.py: what is due, study blocks, the Day hook."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from service.study import day_hook, plan

TZ = "America/Los_Angeles"
ZONE = ZoneInfo(TZ)
DAY = "2026-11-16"   # a Monday


def at(h, mi=0, d=16, mo=11, y=2026):
    return datetime(y, mo, d, h, mi, tzinfo=ZONE).timestamp()


def item(id_, title, due, *, kind="assignment", status="upcoming", course_code=None, course_name=None,
         sample=False, all_day=False):
    return {"id": id_, "course_id": "canvas:1", "title": title, "kind": kind, "due_ts": due,
            "all_day": all_day, "points": 10.0, "score": None, "status": status, "url": None,
            "source": "canvas", "sample": sample, "course_code": course_code, "course_name": course_name}


# ---------------------------------------------------------------- day_summary

def test_summary_due_today_line_and_counts():
    now = at(9, 10)
    rows = [
        item("a", "Lab 5", at(23, 59), course_code="CS 101", course_name="Introduction to Programming"),
        item("b", "Essay draft", at(17), course_name="Writing and Rhetoric: Voices"),
        item("c", "Worksheet 2", at(23, 59, d=17)),
        item("d", "Old lab", at(23, 59, d=13), status="missing"),
        item("e", "HW4", at(12), status="submitted"),
        item("f", "Chapter 6 Quiz", at(10, d=19), kind="quiz", course_name="Statistics"),
    ]
    s = plan.day_summary(rows, now=now, timezone=TZ)
    assert [d["id"] for d in s["due_today"]] == ["b", "a"]
    assert set(s["due_today"][0]) == {"id", "title", "course_name", "due_ts", "all_day", "kind", "status"}
    assert s["due_tomorrow_count"] == 1
    assert s["due_week_count"] == 4          # a, b, c, f (not submitted, not past)
    assert s["missing_count"] == 1
    assert s["next_test"] == {"id": "f", "title": "Chapter 6 Quiz", "course_name": "Statistics",
                              "due_ts": at(10, d=19)}
    # The long name gets no brackets; a test within 3 days is appended.
    assert s["line"] == "2 due today: Essay draft, Lab 5 (CS 101). Quiz Thursday: Chapter 6 Quiz"
    assert len(s["line"]) <= 110
    assert s["sample"] is False


def test_summary_tomorrow_only_and_empty():
    now = at(8)
    s = plan.day_summary([item("c", "Worksheet 2", at(23, 59, d=17))], now=now, timezone=TZ)
    assert s["line"] == "Nothing due today. 1 due tomorrow: Worksheet 2"
    assert s["due_today"] == [] and s["next_test"] is None

    empty = plan.day_summary([item("z", "Far away", at(23, 59, d=30)),
                              item("y", "Done", at(20), status="graded")], now=now, timezone=TZ)
    assert empty["line"] == "" and empty["due_today"] == [] and empty["due_week_count"] == 0
    assert plan.day_summary([], now=now, timezone=TZ)["sample"] is False


def test_summary_caps_titles_and_length():
    now = at(8)
    rows = [item(str(i), f"Problem set number {i} with a rather long title", at(10 + i)) for i in range(5)]
    s = plan.day_summary(rows, now=now, timezone=TZ)
    assert s["line"].startswith("5 due today: ")
    assert s["line"].endswith(" and 2 more")
    assert len(s["line"]) <= 110


def test_summary_test_alone_and_sample_flag():
    now = at(8)
    s = plan.day_summary([item("q", "Midterm 2", at(9, d=18), kind="exam", sample=True)], now=now, timezone=TZ)
    assert s["line"] == "Exam Wednesday: Midterm 2"
    assert s["sample"] is True
    s2 = plan.day_summary([item("q", "Midterm 2", at(9, d=17), kind="exam")], now=now, timezone=TZ)
    assert s2["line"] == "Nothing due today. 1 due tomorrow: Midterm 2"   # listed already, not repeated


# ---------------------------------------------------------------- deadlines

def test_deadlines_for_the_day():
    rows = [item("a", "Lab 5", at(23, 59), course_code="CS 101"),
            item("b", "Essay", at(9), course_name="Writing"),
            item("c", "Done", at(12), status="submitted"),
            item("d", "Tomorrow", at(9, d=17)),
            item("e", "No course", at(10), all_day=True),
            item("f", "Undated", None)]
    out = plan.deadlines(rows, day=DAY, timezone=TZ)
    assert out == [
        {"id": "study:b", "title": "Essay · Writing", "due_ts": at(9), "source": "study"},
        {"id": "study:e", "title": "No course", "due_ts": at(10), "source": "study", "all_day": True,
         "due_date": DAY},
        {"id": "study:a", "title": "Lab 5 · CS 101", "due_ts": at(23, 59), "source": "study"},
    ]
    assert all(isinstance(d["due_ts"], float) for d in out)


# ---------------------------------------------------------------- study_blocks

def test_blocks_first_free_gaps_around_busy():
    now = at(9, 10)                                  # rounds up to 09:30
    rows = [item("a", "Lab 5", at(23, 59)), item("b", "Essay", at(17, d=17)), item("c", "Third", at(18))]
    busy = [(at(9), at(10)), (at(10, 30), at(12))]
    blocks = plan.study_blocks(rows, busy, now=now, timezone=TZ, day=DAY)
    assert len(blocks) == 2                           # at most two
    # Due order: c (18:00 today) first, then a; b is later.
    assert [b["id"] for b in blocks] == ["study-block:c", "study-block:a"]
    assert blocks[0]["start"] == at(12) and blocks[0]["end"] == at(12, 45)
    assert blocks[1]["start"] == at(12, 45) and blocks[1]["end"] == at(13, 30)
    for b in blocks:
        assert b["kind"] == "study" and b["task_id"] is None and b["warnings"] == []
        assert b["title"].startswith("Study: ")
        assert isinstance(b["start"], float) and isinstance(b["end"], float)
        assert b["end"] - b["start"] == 45 * 60
        for s, e in busy:
            assert b["end"] <= s or b["start"] >= e


def test_blocks_respect_window_due_time_and_status():
    # Before 08:00: start at 08:00.
    early = plan.study_blocks([item("a", "Lab", at(23, 59))], [], now=at(6, 5), timezone=TZ, day=DAY)
    assert early[0]["start"] == at(8)
    # 21:20 -> 21:30, ends 22:15 > 22:00: no block.
    assert plan.study_blocks([item("a", "Lab", at(23, 59))], [], now=at(21, 20), timezone=TZ, day=DAY) == []
    # 21:00 -> fits exactly to 21:45.
    assert plan.study_blocks([item("a", "Lab", at(23, 59))], [], now=at(21), timezone=TZ, day=DAY)[0]["start"] == at(21)
    # Must end before the due time.
    assert plan.study_blocks([item("a", "Lab", at(10))], [], now=at(9, 20), timezone=TZ, day=DAY) == []
    # Submitted, past due, more than 48 hours away, or undated: nothing.
    rows = [item("s", "Sent", at(20), status="submitted"), item("p", "Past", at(8), status="missing"),
            item("f", "Far", at(12, d=19)), item("u", "Undated", None)]
    assert plan.study_blocks(rows, [], now=at(9), timezone=TZ, day=DAY) == []
    # Only for today.
    assert plan.study_blocks([item("a", "Lab", at(23, 59, d=17))], [], now=at(9), timezone=TZ,
                             day="2026-11-17") == []


def test_blocks_skip_when_the_day_is_full_and_handle_open_ended_busy():
    full = [(at(8), at(22))]
    assert plan.study_blocks([item("a", "Lab", at(23, 59))], full, now=at(9), timezone=TZ, day=DAY) == []
    # A busy block with no end is treated as an hour.
    out = plan.study_blocks([item("a", "Lab", at(23, 59))], [(at(9), None)], now=at(9), timezone=TZ, day=DAY)
    assert out[0]["start"] == at(10)


# ---------------------------------------------------------------- day_hook.attach

class FakeStore:
    def __init__(self, courses, assignments):
        self._c, self._a = courses, assignments

    def courses(self):
        return list(self._c)

    def assignments(self):
        return list(self._a)


def snapshot(now):
    blocks = [{"id": "cal:1", "title": "Class", "start": at(9), "end": at(10, 30), "kind": "calendar",
               "task_id": None, "warnings": []}]
    deadlines = [{"id": "rem:1", "title": "Pay rent", "due_ts": at(12), "source": "reminders"}]
    return {"day": DAY, "timezone": TZ, "blocks": blocks, "deadlines": deadlines}


def test_attach_adds_study_without_mutating_planner_lists(tmp_path, monkeypatch):
    db = tmp_path / "study" / "study.db"
    db.parent.mkdir()
    db.write_bytes(b"")
    monkeypatch.setattr(day_hook, "store_path", lambda: db)
    rows = [{"id": "canvas:1:aa", "course_id": "canvas:1", "title": "Lab 5", "kind": "assignment",
             "due_ts": at(23, 59), "all_day": False, "points": 20.0, "score": None, "status": "upcoming",
             "url": None, "source": "canvas", "sample": False}]
    courses = [{"id": "canvas:1", "name": "Introduction to Programming", "code": "CS 101"}]
    monkeypatch.setattr(day_hook, "study_store", lambda: FakeStore(courses, rows))
    result = snapshot(at(9, 5))
    planner_blocks, planner_deadlines = result["blocks"], result["deadlines"]
    day_hook.attach(result, now=at(9, 5), timezone=TZ)
    assert result["study"]["line"] == "1 due today: Lab 5 (CS 101)"
    assert result["study"]["due_today"][0]["course_name"] == "Introduction to Programming"
    assert len(planner_blocks) == 1 and len(planner_deadlines) == 1    # untouched
    assert [d["id"] for d in result["deadlines"]] == ["rem:1", "study:canvas:1:aa"]
    assert result["deadlines"][1]["title"] == "Lab 5 · CS 101"
    assert [b["id"] for b in result["blocks"]] == ["cal:1", "study-block:canvas:1:aa"]
    assert result["blocks"][1]["start"] == at(10, 30)


def test_attach_does_nothing_without_a_store_file(tmp_path, monkeypatch):
    missing = tmp_path / "study" / "study.db"
    monkeypatch.setattr(day_hook, "store_path", lambda: missing)

    def boom():
        raise AssertionError("must not open the store")
    monkeypatch.setattr(day_hook, "study_store", boom)
    result = snapshot(at(9))
    before = {k: v for k, v in result.items()}
    day_hook.attach(result, now=at(9), timezone=TZ)
    assert result == before and "study" not in result
    assert not missing.parent.exists()


def test_attach_never_raises(tmp_path, monkeypatch):
    db = tmp_path / "study.db"
    db.write_bytes(b"")
    monkeypatch.setattr(day_hook, "store_path", lambda: db)

    def broken():
        raise RuntimeError("disk")
    monkeypatch.setattr(day_hook, "study_store", broken)
    result = snapshot(at(9))
    day_hook.attach(result, now=at(9), timezone=TZ)
    assert "study" not in result
    day_hook.attach({"day": None, "blocks": None}, now=at(9), timezone="Not/AZone")


def test_attach_with_the_real_store(tmp_path, monkeypatch):
    store_mod = pytest.importorskip("service.study.store")
    db = tmp_path / "study" / "study.db"
    store = store_mod.StudyStore(db)
    store.upsert_courses([{"id": "canvas:9", "name": "Statistics", "code": "STAT 101", "grade": None,
                           "score": None, "url": None, "source": "canvas", "sample": False}])
    store.upsert_assignments([{"id": "canvas:9:bb", "course_id": "canvas:9", "title": "HW2", "kind": "assignment",
                               "due_ts": at(23, 59, d=17), "all_day": False, "points": 20.0, "score": None,
                               "status": "upcoming", "details": "", "url": None, "source": "canvas",
                               "sample": False}])
    monkeypatch.setattr(day_hook, "store_path", lambda: db)
    monkeypatch.setattr(day_hook, "study_store", lambda: store)
    result = snapshot(at(9))
    day_hook.attach(result, now=at(9), timezone=TZ)
    assert result["study"]["line"] == "Nothing due today. 1 due tomorrow: HW2 (STAT 101)"
    assert [d["id"] for d in result["deadlines"]] == ["rem:1"]          # nothing due on this day
    assert [b["id"] for b in result["blocks"]] == ["cal:1", "study-block:canvas:9:bb"]


def test_attach_after_a_merged_import_and_with_a_missing_course_row(tmp_path, monkeypatch):
    """The Dashboard (name-keyed course) and the course page (numeric id) converge on one course,
    so every due item names it; an item whose course row is gone still shows, with no name."""
    store_mod = pytest.importorskip("service.study.store")
    db = tmp_path / "study" / "study.db"
    store = store_mod.StudyStore(db)
    name, key = "Introduction to Programming", "introduction to programming"
    row = {"kind": "assignment", "all_day": False, "points": None, "score": None, "status": "upcoming",
           "details": "", "url": None, "source": "canvas", "sample": False}
    store.import_rows([{"id": "canvas:0a1b2c3d4e5f", "name": name, "merge_key": key}],
                      [row | {"id": "canvas:dddddddddddd", "course_id": "canvas:0a1b2c3d4e5f",
                              "title": "Lab 5: Lists", "due_ts": at(23, 59)}])
    store.import_rows([{"id": "canvas:12345", "name": "CS 101: " + name, "code": "CS 101",
                        "merge_key": "cs 101 " + key, "url": "https://canvas.example.edu/courses/12345"}],
                      [row | {"id": "canvas:12345:eeeeeeeeeeee", "course_id": "canvas:12345",
                              "title": "Lab 5: Lists", "due_ts": at(23, 59), "points": 20.0},
                       row | {"id": "canvas:12345:ffffffffffff", "course_id": "canvas:12345",
                              "title": "Quiz 3", "kind": "quiz", "due_ts": at(20)}])
    store.upsert_assignments([row | {"id": "canvas:404:aa", "course_id": "canvas:404", "title": "Orphan",
                                     "due_ts": at(21)}])
    assert [c["id"] for c in store.courses()] == ["canvas:12345"]
    monkeypatch.setattr(day_hook, "store_path", lambda: db)
    monkeypatch.setattr(day_hook, "study_store", lambda: store)
    result = snapshot(at(9))
    day_hook.attach(result, now=at(9), timezone=TZ)
    due = {d["title"]: d["course_name"] for d in result["study"]["due_today"]}
    assert due == {"Quiz 3": "CS 101: " + name, "Orphan": "", "Lab 5: Lists": "CS 101: " + name}
    assert result["study"]["next_test"]["course_name"] == "CS 101: " + name
    titles = {d["id"]: d["title"] for d in result["deadlines"] if d["source"] == "study"}
    assert titles == {"study:canvas:dddddddddddd": "Lab 5: Lists · CS 101",
                      "study:canvas:12345:ffffffffffff": "Quiz 3 · CS 101",
                      "study:canvas:404:aa": "Orphan"}


# ---------------------------------------------------------------- upcoming

def test_upcoming_window_edges_and_order():
    now = at(9, 10)
    rows = [
        item("early", "Earlier today", at(0), course_code="CS 101"),            # start of today: in
        item("late", "Still open", at(8), course_name="Statistics"),             # earlier today, open: in
        item("yday", "Yesterday", at(23, 59, d=15)),                            # before today: out
        item("d7", "Day 7", at(23, 59, d=23)),                                  # end of day+7: in
        item("d8", "Day 8", at(0, d=24)),                                       # start of day+8: out
        item("mid", "Midweek", at(12, d=18), course_name="A very long course name indeed"),
    ]
    s = plan.day_summary(rows, now=now, timezone=TZ)
    assert [u["id"] for u in s["upcoming"]] == ["early", "late", "mid", "d7"]
    assert s["upcoming_more"] == 0
    assert [d["id"] for d in s["due_today"]] == ["early", "late"]       # today is in both lists


def test_upcoming_item_shape_and_labels():
    now = at(9)
    rows = [item("a", "Lab 5", at(23, 59), course_code="CS 101", course_name="Programming"),
            item("b", "Essay", at(23, 59, d=17), course_name="Statistics"),
            item("c", "Reading", at(23, 59, d=18), course_name="Writing and Rhetoric: Voices"),
            item("d", "Notes", at(23, 59, d=19)),
            item("e", "Reflection", at(0, d=20), all_day=True, kind="discussion")]
    ups = {u["id"]: u for u in plan.day_summary(rows, now=now, timezone=TZ)["upcoming"]}
    assert ups["a"] == {"id": "a", "title": "Lab 5", "course_name": "Programming", "course_label": "CS 101",
                        "due_ts": at(23, 59), "all_day": False, "kind": "assignment", "status": "upcoming"}
    assert ups["b"]["course_label"] == "Statistics"
    assert ups["c"]["course_label"] == "" and ups["d"]["course_label"] == ""
    assert ups["e"]["all_day"] is True and ups["e"]["kind"] == "discussion"
    assert set(ups["a"]) == {"id", "title", "course_name", "course_label", "due_ts", "all_day", "kind", "status"}
    assert not {"points", "score", "url", "details"} & set(ups["a"])


def test_upcoming_cap_and_more():
    now = at(9)
    rows = [item(f"r{i:02}", f"T{i}", at(10 + i % 10, d=16 + i // 10)) for i in range(12)]
    s = plan.day_summary(rows, now=now, timezone=TZ)
    assert plan.UPCOMING_CAP == 8
    assert len(s["upcoming"]) == 8 and s["upcoming_more"] == 4
    assert [u["due_ts"] for u in s["upcoming"]] == sorted(u["due_ts"] for u in s["upcoming"])
    assert s["upcoming"][0]["id"] == "r00"


def test_upcoming_leaves_out_done_work_and_old_missing():
    now = at(9)
    rows = [item("s", "Sent", at(12), status="submitted"), item("g", "Graded", at(12, d=17), status="graded"),
            item("m", "Old missing", at(12, d=10), status="missing"),
            item("k", "Open", at(12, d=17)), item("n", "No date", None)]
    s = plan.day_summary(rows, now=now, timezone=TZ)
    assert [u["id"] for u in s["upcoming"]] == ["k"]
    assert s["missing_count"] == 1


def test_upcoming_empty():
    s = plan.day_summary([], now=at(9), timezone=TZ)
    assert s["upcoming"] == [] and s["upcoming_more"] == 0


def test_upcoming_across_a_dst_change_week():
    # US clocks fall back on Sunday 2026-11-01: today Mon 2026-10-26, day+7 is Mon 2026-11-02 (a 25 h week).
    def on(h, mi=0, d=26, mo=10):
        return datetime(2026, mo, d, h, mi, tzinfo=ZONE).timestamp()
    now = on(9)
    rows = [item("in", "Last night", datetime(2026, 11, 2, 23, 59, tzinfo=ZONE).timestamp()),
            item("out", "Next day", datetime(2026, 11, 3, 0, 0, tzinfo=ZONE).timestamp()),
            item("t", "Today", on(0))]
    s = plan.day_summary(rows, now=now, timezone=TZ)
    assert [u["id"] for u in s["upcoming"]] == ["t", "in"]
    # Spring forward: Sunday 2027-03-14; today Mon 2027-03-08 -> day+7 is Mon 2027-03-15 (a 23 h week).
    spring = lambda h, mi, d: datetime(2027, 3, d, h, mi, tzinfo=ZONE).timestamp()  # noqa: E731
    rows = [item("in", "Edge", spring(23, 59, 15)), item("out", "Past edge", spring(0, 0, 16))]
    s = plan.day_summary(rows, now=spring(9, 0, 8), timezone=TZ)
    assert [u["id"] for u in s["upcoming"]] == ["in"]


def test_deadlines_and_blocks_gain_slides_only_when_asked():
    rows = [item("a", "Lab 5", at(23, 59))]
    assert "slides" not in plan.deadlines(rows, day=DAY, timezone=TZ)[0]
    found = {"a": ([{"source_id": "s", "deck": "Lecture 2", "pages": [3]}], "Slide 3 of Lecture 2 covers it.",
                   [{"source_id": "s", "page": 3}])}
    d = plan.deadlines(rows, day=DAY, timezone=TZ, slides=found)[0]
    assert d["covers_text"] == "Slide 3 of Lecture 2 covers it."
    assert d["review"] == {"minutes": 15, "pages": [{"source_id": "s", "page": 3}]}
    other = plan.deadlines([item("b", "Lab 6", at(22))], day=DAY, timezone=TZ, slides=found)[0]
    assert other["slides"] == [] and other["covers_text"] is None and "review" not in other
    b = plan.study_blocks(rows, [], now=at(9), timezone=TZ, day=DAY, slides=found)[0]
    assert b["slides"] == found["a"][0] and "review" not in b


def test_weak_items_need_two_misses_and_say_how_many_slides():
    now = at(9)
    misses = [{"material_id": "m1", "topic": "Recursion", "source_id": "s", "page": 4, "at": now - 60},
              {"material_id": "m1", "topic": "recursion", "source_id": "s", "page": 4, "at": now - 30},
              {"material_id": "m1", "topic": "Loops", "source_id": None, "page": None, "at": now},
              {"material_id": "m1", "topic": "Loops", "source_id": None, "page": None, "at": now},
              {"material_id": "m1", "topic": "Sets", "source_id": "s", "page": 1, "at": now},
              {"material_id": "m1", "topic": "Old", "at": now - 30 * 86400},
              {"material_id": "m1", "topic": "Old", "at": now - 30 * 86400}]
    got = plan.weak_items(misses, course_labels={"c1": "CS 101"}, material_courses={"m1": "c1"},
                          decks={"s": "Week 3"}, now=now)
    assert [w["text"] for w in got] == ["You missed 2 on Loops. A quick review would help.",
                                        "You missed 2 on Recursion; here is that slide again."]
    assert got[1]["slides"] == [{"source_id": "s", "deck": "Week 3", "pages": [4]}] and got[1]["course"] == "CS 101"


# ---------------------------------------------------------------- one school item, once

def twin(id_, title, due, *, course_id="canvas:1", course_name=None, course_code=None, status="upcoming",
         all_day=False, url=None):
    return item(id_, title, due, status=status, course_name=course_name, course_code=course_code,
                all_day=all_day) | {"course_id": course_id, "url": url}


def test_title_key_drops_only_a_section_code():
    assert plan.title_key("  Worksheet 2 [STAT-101-02] ") == "worksheet 2"
    assert plan.title_key("Week 3 Reading:  Functions [CS-101-04]") == "week 3 reading: functions"
    assert plan.title_key("Week 3 Reading: Functions") == "week 3 reading: functions"
    assert plan.title_key("Call the bank [urgent]") == "call the bank [urgent]"
    assert plan.title_key("Pick up [2] boxes") == "pick up [2] boxes"
    assert plan.title_key("Read [Chapter 5]") == "read [chapter 5]"


def test_twins_show_once_in_the_line_upcoming_deadlines_and_blocks():
    """Bugs 1 and 2: one reading stored twice (named course and nameless page) shows once."""
    now = at(16, 10)
    rows = [twin("canvas:12345:aa", "Week 3 Reading: Functions", at(23, 59), course_id="canvas:12345"),
            twin("canvas:0a1b:bb", "Week 3 Reading: Functions", at(23, 59), course_id="canvas:0a1b",
                 course_name="Introduction to Programming", course_code="CS 101")]
    s = plan.day_summary(rows, now=now, timezone=TZ)
    assert s["line"] == "1 due today: Week 3 Reading: Functions (CS 101)"
    assert [d["id"] for d in s["due_today"]] == ["canvas:0a1b:bb"]
    assert [u["course_label"] for u in s["upcoming"]] == ["CS 101"]
    got = plan.deadlines(rows, day=DAY, timezone=TZ)
    assert [(d["id"], d["title"]) for d in got] == [("study:canvas:0a1b:bb", "Week 3 Reading: Functions · CS 101")]
    blocks = plan.study_blocks(rows, [], now=now, timezone=TZ, day=DAY)
    assert [b["id"] for b in blocks] == ["study-block:canvas:0a1b:bb"]
    # The same in the other order: the survivor (and so the block id) doesn't depend on it.
    assert [b["id"] for b in plan.study_blocks(rows[::-1], [], now=now, timezone=TZ, day=DAY)] == [blocks[0]["id"]]


def test_twins_across_page_kinds_all_day_section_suffix_and_no_course():
    now = at(9)
    rows = [twin("a", "Worksheet 2", at(23, 59), course_name="Statistics", course_code="STAT 101"),
            twin("b", "worksheet  2 [STAT-101-02]", at(0), all_day=True, course_id=None),
            twin("c", "Worksheet 2", at(23, 59, d=17), course_name="Statistics")]   # another day: its own
    s = plan.day_summary(rows, now=now, timezone=TZ)
    assert [d["id"] for d in s["due_today"]] == ["a"] and [u["id"] for u in s["upcoming"]] == ["a", "c"]


def test_two_timed_copies_at_different_times_or_different_courses_stay_two():
    now = at(9)
    rows = [twin("a", "Quiz 1", at(10), course_name="Statistics"),
            twin("b", "Quiz 1", at(14), course_name="Statistics"),
            twin("c", "Quiz 1", at(23, 59), course_id="canvas:2", course_name="Introduction to Programming"),
            twin("d", "Quiz 1", at(23, 59), course_id="canvas:3", course_name="Statistics")]
    s = plan.day_summary(rows, now=now, timezone=TZ)
    assert [d["id"] for d in s["due_today"]] == ["a", "b", "c", "d"]
    assert plan.day_summary(rows[2:], now=now, timezone=TZ)["line"].startswith("2 due today")


def test_a_handed_in_twin_keeps_the_item_done():
    now = at(9)
    rows = [twin("a", "Worksheet 2", at(23, 59), course_name="Statistics"),
            twin("b", "Worksheet 2", at(23, 59), course_id="canvas:9", status="submitted")]
    s = plan.day_summary(rows, now=now, timezone=TZ)
    assert s["due_today"] == [] and s["upcoming"] == [] and "due today" not in s["line"]
    assert plan.deadlines(rows, day=DAY, timezone=TZ) == []
    assert plan.study_blocks(rows, [], now=now, timezone=TZ, day=DAY) == []
    graded = [rows[0], rows[1] | {"status": "graded"}, twin("c", "Worksheet 2", at(23, 59), status="submitted")]
    assert plan.collapse(graded, timezone=TZ)[0]["status"] == "graded"


def test_a_booking_under_a_twins_id_is_still_recognised():
    """Block ids: the survivor's id is stable; a booking made under the other twin's id keeps it."""
    from service.study import blocks_api
    now = at(16)
    rows = [twin("canvas:12345:aa", "Lab 5", at(23, 59), course_id="canvas:12345"),
            twin("canvas:0a1b:bb", "Lab 5", at(23, 59), course_id="canvas:0a1b", course_name="Statistics")]
    blocks = plan.study_blocks(rows, [], now=now, timezone=TZ, day=DAY, booked=["study-block:canvas:12345:aa"])
    assert [b["id"] for b in blocks] == ["study-block:canvas:12345:aa"]
    booked = {"study-block:canvas:12345:aa": {"source_id": "ev1", "title": "Study: Lab 5", "start": at(18),
                                              "end": at(18, 45), "day": DAY}}
    shown, _ = blocks_api.mark(blocks, [], DAY, booked)
    assert [(b["id"], b["added"], b["start"]) for b in shown] == [("study-block:canvas:12345:aa", True, at(18))]
    # The booked twin was folded away since: the survivor's fresh block isn't offered again.
    fresh = plan.study_blocks(rows[1:], [], now=now, timezone=TZ, day=DAY)
    shown, _ = blocks_api.mark(fresh, [], DAY, booked)
    assert [(b["id"], b["added"]) for b in shown] == [("study-block:canvas:12345:aa", True)]


def test_attach_twins_with_unmatched_scores_show_once(tmp_path, monkeypatch):
    """The store can't merge a nameless course page whose score differs from the Dashboard's
    (36385890 needs the scores to match), so the plan's own collapse is what shows it once."""
    store_mod = pytest.importorskip("service.study.store")
    db = tmp_path / "study" / "study.db"
    store = store_mod.StudyStore(db)
    row = {"kind": "assignment", "all_day": False, "points": None, "score": None, "status": "upcoming",
           "details": "", "url": None, "source": "canvas", "sample": False}
    store.import_rows([{"id": "canvas:0a1b2c3d4e5f", "name": "Introduction to Programming", "code": "CS 101",
                        "score": 92.0, "merge_key": "introduction to programming"}],
                      [row | {"id": "canvas:dddddddddddd", "course_id": "canvas:0a1b2c3d4e5f",
                              "title": "Week 3 Reading: Functions", "due_ts": at(23, 59)}])
    store.import_rows([{"id": "canvas:12345", "name": "", "score": 91.5,
                        "url": "https://canvas.example.edu/courses/12345"}],
                      [row | {"id": "canvas:12345:eeeeeeeeeeee", "course_id": "canvas:12345",
                              "title": "Week 3 Reading: Functions", "due_ts": at(23, 59)}])
    assert len(store.courses()) == 2 and len(store.assignments()) == 2     # still split in the store
    monkeypatch.setattr(day_hook, "store_path", lambda: db)
    monkeypatch.setattr(day_hook, "study_store", lambda: store)
    result = snapshot(at(16, 10))
    day_hook.attach(result, now=at(16, 10), timezone=TZ)
    assert result["study"]["line"] == "1 due today: Week 3 Reading: Functions (CS 101)"
    assert [d["id"] for d in result["deadlines"] if d["source"] == "study"] == ["study:canvas:dddddddddddd"]
    assert [b["id"] for b in result["blocks"] if b["kind"] == "study"] == ["study-block:canvas:dddddddddddd"]


def workspace_snapshot():
    result = snapshot(at(9))
    feed = {"id": "cal:feed", "title": "Worksheet 2 [STAT-101-02]", "start": at(23, 59), "end": at(23, 59),
            "kind": "calendar", "task_id": None, "warnings": []}
    result["blocks"] = [*result["blocks"], feed]
    result["all_day"] = [{"id": "cal:allday", "title": "Week 3 Reading: Functions [CS-101-04]", "start": at(0),
                          "end": at(0, d=17), "kind": "calendar", "task_id": None, "warnings": []}]
    result["deadlines"] = [*result["deadlines"],
                           {"id": "rem:feed", "title": "Worksheet 2 [STAT-101-02]", "due_ts": at(23, 59),
                            "source": "reminders"},
                           {"id": "rem:other", "title": "Worksheet 2 [STAT-101-02]", "due_ts": at(23, 59, d=18),
                            "source": "reminders"},
                           {"id": "rem:mine", "title": "Worksheet 2 notes [urgent]", "due_ts": at(23, 59),
                            "source": "reminders"}]
    suggestion = {"id": "sug:1", "title": "Read: newsletter", "start": at(10, 30), "end": at(11, 30),
                  "kind": "suggestion", "task_id": None, "warnings": []}
    result["workspace"] = {
        "attention": [{"id": "rem:feed", "title": "Worksheet 2 [STAT-101-02]", "source": "reminders",
                       "due_ts": at(23, 59)},
                      {"id": "rem:rent", "title": "Pay rent", "source": "reminders", "due_ts": at(12)}],
        "needs_you": {"items": [{"id": "rem:feed", "kind": "due", "text": "Worksheet 2 [STAT-101-02]",
                                 "due_ts": at(23, 59), "all_day": False}], "more": 0},
        "proposed_plan": {"blocks": [result["blocks"][0], suggestion, feed], "unscheduled": [], "warnings": []}}
    return result


def test_attach_drops_calendar_feed_copies_of_school_items(tmp_path, monkeypatch):
    """Bug 3: Canvas's calendar feed adds "[SECTION-CODE]"; its copy shows once, as the school item."""
    db = tmp_path / "study.db"
    db.write_bytes(b"")
    monkeypatch.setattr(day_hook, "store_path", lambda: db)
    rows = [twin("canvas:1:ws2", "Worksheet 2", at(23, 59)),
            twin("canvas:2:w3", "Week 3 Reading: Functions", at(23, 59), course_id="canvas:2")]
    courses = [{"id": "canvas:1", "name": "Statistics", "code": "STAT 101"},
               {"id": "canvas:2", "name": "Introduction to Programming", "code": "CS 101"}]
    monkeypatch.setattr(day_hook, "study_store", lambda: FakeStore(courses, rows))
    result = workspace_snapshot()
    planner_deadlines, planner_blocks = result["deadlines"], result["blocks"]
    day_hook.attach(result, now=at(9), timezone=TZ)
    assert [d["id"] for d in result["deadlines"]] == [
        "rem:1", "rem:mine", "study:canvas:1:ws2", "study:canvas:2:w3", "rem:other"]
    assert result["all_day"] == []
    assert "cal:feed" not in [b["id"] for b in result["blocks"]]
    assert [a["id"] for a in result["workspace"]["attention"]] == ["rem:rent"]
    assert result["workspace"]["needs_you"]["items"] == []
    assert [b["id"] for b in result["workspace"]["proposed_plan"]["blocks"]] == ["cal:1", "sug:1"]
    assert len(planner_deadlines) == 4 and len(planner_blocks) == 2       # the planner's lists stay as built


def test_attach_keeps_a_study_block_clear_of_suggested_blocks(tmp_path, monkeypatch):
    """Bug 4: Day draws the workspace's proposed plan, so its Suggested block is busy time too."""
    db = tmp_path / "study.db"
    db.write_bytes(b"")
    monkeypatch.setattr(day_hook, "store_path", lambda: db)
    rows = [twin("canvas:1:lab", "Lab 5", at(23, 59))]
    monkeypatch.setattr(day_hook, "study_store", lambda: FakeStore([], rows))
    result = workspace_snapshot()
    day_hook.attach(result, now=at(9), timezone=TZ)
    study = [b for b in result["blocks"] if b["kind"] == "study"]
    assert [(b["start"], b["end"]) for b in study] == [(at(11, 30), at(12, 15))]
    shown = [b for b in [*result["workspace"]["proposed_plan"]["blocks"], *study]]
    spans = sorted((b["start"], b["end"]) for b in shown if b["end"] > b["start"])
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_study_plan.py"' in manifest
