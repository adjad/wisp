"""The Study store (service/study/store.py) and the built-in sample (service/study/sample.py):
merging upserts, ordering, the material lifecycle, best scores, and clearing."""
from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from service.study import sample
from service.study.store import StudyStore, merge_key

ZONE = "America/Los_Angeles"
NOW = datetime(2026, 11, 10, 9, 30, tzinfo=ZoneInfo(ZONE)).timestamp()


@pytest.fixture
def store(tmp_path):
    return StudyStore(tmp_path / "study" / "study.db")


def course(cid="canvas:1", **values):
    return {"id": cid, "name": "Introduction to Programming", "code": "CS 101", "grade": "B+",
            "score": 87.5, "url": "https://school.instructure.com/courses/1", "source": "canvas",
            "sample": False} | values


def item(aid="canvas:1:5", due=NOW + 3600, **values):
    return {"id": aid, "course_id": "canvas:1", "title": "Lab 5: Lists", "kind": "assignment", "due_ts": due,
            "all_day": False, "points": 20.0, "score": None, "status": "upcoming", "details": "Use lists.",
            "url": None, "source": "canvas", "sample": False} | values


def quiz_body(n=4):
    return {"questions": [{"prompt": f"Q{i}", "choices": ["a", "b", "c", "d"], "answer": 0, "why": "w"}
                          for i in range(n)]}


def test_the_file_and_folder_are_private(tmp_path):
    StudyStore(tmp_path / "study" / "study.db")
    assert stat.S_IMODE(os.stat(tmp_path / "study").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "study" / "study.db").st_mode) == 0o600


def test_upserting_twice_keeps_one_row_and_updates_it(store):
    assert store.upsert_courses([course()]) == 1
    assert store.upsert_courses([course(grade="B+", score=88.0)]) == 1
    rows = store.courses()
    assert len(rows) == 1 and rows[0]["grade"] == "B+" and rows[0]["score"] == 88.0
    assert rows[0]["sample"] is False and "updated_at" not in rows[0]


def test_a_page_that_leaves_a_field_out_never_blanks_it(store):
    store.upsert_courses([course()])
    store.upsert_courses([{"id": "canvas:1", "name": "Introduction to Programming", "grade": None}])
    assert store.courses()[0]["grade"] == "B+" and store.courses()[0]["code"] == "CS 101"
    store.upsert_assignments([item()])
    store.upsert_assignments([item(details="", score=18.0, status="graded")])
    got = store.assignments()[0]
    assert got["details"] == "Use lists." and got["score"] == 18.0 and got["status"] == "graded"


def test_rows_without_an_id_are_skipped(store):
    assert store.upsert_assignments([item(aid=""), {"title": "no id"}, item()]) == 1


def test_details_are_capped(store):
    store.upsert_assignments([item(details="x" * 9000)])
    assert len(store.assignments()[0]["details"]) == 4000


def test_assignments_come_back_due_first_with_undated_last(store):
    store.upsert_assignments([item("a", due=None, title="Undated"), item("b", due=NOW + 50),
                              item("c", due=NOW + 10), item("d", due=NOW - 10)])
    assert [r["id"] for r in store.assignments()] == ["d", "c", "b", "a"]
    assert store.assignments()[0]["all_day"] is False


def test_a_material_is_saved_read_listed_and_deleted(store):
    saved = store.save_material({"kind": "quiz", "title": "Lists quiz", "course_id": "canvas:1",
                                 "assignment_id": None, "model": "m1", "body": quiz_body(5)})
    assert saved["id"].startswith("m_") and saved["created_at"] > 0 and saved["count"] == 5
    assert saved["body"] == quiz_body(5) and saved["model"] == "m1" and saved["best_score"] is None
    assert store.material(saved["id"]) == saved
    older = store.save_material({"kind": "flashcards", "title": "Cards", "created_at": 1.0,
                                 "body": {"cards": [{"front": "f", "back": "b"}] * 4}})
    listed = store.materials()
    assert [m["id"] for m in listed] == [saved["id"], older["id"]]
    assert "body" not in listed[0] and listed[1]["count"] == 4
    assert store.delete_material(saved["id"]) is True
    assert store.delete_material(saved["id"]) is False
    assert store.material(saved["id"]) is None


def test_attempts_keep_the_best_score(store):
    mid = store.save_material({"kind": "quiz", "title": "Q", "body": quiz_body()})["id"]
    assert store.record_attempt(mid, 2, 4) == 0.5
    assert store.record_attempt(mid, 3, 4) == 0.75
    assert store.record_attempt(mid, 1, 4) == 0.75
    assert store.material(mid)["best_score"] == 0.75 and store.materials()[0]["best_score"] == 0.75
    assert store.record_attempt("nope", 1, 1) is None
    assert store.record_attempt(mid, 0, 0) is None


def test_meta_round_trips_json(store):
    assert store.meta("last_import") is None and store.meta("x", 5) == 5
    store.set_meta("last_import", 123.5)
    assert store.meta("last_import") == 123.5


def test_clear_wipes_everything(store):
    store.upsert_courses([course()])
    store.upsert_assignments([item()])
    store.save_material({"kind": "quiz", "title": "Q", "body": quiz_body()})
    store.set_meta("last_import", 1.0)
    store.clear()
    assert store.courses() == [] and store.assignments() == [] and store.materials() == []
    assert store.meta("last_import") is None


def test_the_sample_loads_valid_synthetic_rows(store):
    sample.load(store, now=NOW, timezone=ZONE)
    courses, items, mats = store.courses(), store.assignments(), store.materials()
    assert len(courses) == 4 and 9 <= len(items) <= 12 and len(mats) == 4
    for row in courses + items + mats:
        assert row["id"].startswith("sample:") and row["sample"] is True
    assert store.has_sample()
    zone = ZoneInfo(ZONE)
    today = datetime.fromtimestamp(NOW, zone).date()
    due_days = [(datetime.fromtimestamp(i["due_ts"], zone).date() - today).days for i in items]
    assert due_days.count(0) == 2 and 1 in due_days and 3 in due_days and 6 in due_days
    tonight = [i for i in items if datetime.fromtimestamp(i["due_ts"], zone).strftime("%H:%M") == "23:59"
               and datetime.fromtimestamp(i["due_ts"], zone).date() == today]
    assert tonight and all(i["due_ts"] > NOW for i in items if i["due_ts"] and
                           datetime.fromtimestamp(i["due_ts"], zone).date() == today)
    assert any(i["status"] == "missing" and NOW - 3 * 86400 < i["due_ts"] < NOW for i in items)
    assert {i["status"] for i in items} >= {"upcoming", "missing", "submitted", "graded"}
    assert {i["kind"] for i in items} >= {"quiz", "exam", "project"}
    assert {m["kind"] for m in mats} == {"guide", "quiz", "lesson", "flashcards"}
    counts = {m["kind"]: m["count"] for m in mats}
    assert counts["quiz"] == 6 and counts["lesson"] == 4 and counts["flashcards"] == 10


def test_sample_material_bodies_fit_the_contract_limits(store):
    sample.load(store, now=NOW, timezone=ZONE)
    for summary in store.materials():
        body = store.material(summary["id"])["body"]
        if summary["kind"] == "guide":
            assert 2 <= len(body["sections"]) <= 8 and body["summary"] and body["key_terms"]
        if summary["kind"] == "quiz":
            assert 3 <= len(body["questions"]) <= 15
            for q in body["questions"]:
                assert 2 <= len(q["choices"]) <= 5 and 0 <= q["answer"] < len(q["choices"])
                assert len(q["prompt"]) <= 600 and all(len(c) <= 300 for c in q["choices"]) and q["why"]
        if summary["kind"] == "lesson":
            assert 2 <= len(body["steps"]) <= 8
            for s in body["steps"]:
                assert s["title"] and len(s["explain"]) <= 2000
                if s["check"] is not None:
                    assert 0 <= s["check"]["answer"] < len(s["check"]["choices"])
        if summary["kind"] == "flashcards":
            assert 4 <= len(body["cards"]) <= 30 and all(c["front"] and c["back"] for c in body["cards"])


def test_the_sample_passes_the_real_validator_when_it_exists(store):
    generate = pytest.importorskip("service.study.generate")
    sample.load(store, now=NOW, timezone=ZONE)
    for summary in store.materials():
        generate.validate(summary["kind"], store.material(summary["id"])["body"])


def test_reloading_the_sample_replaces_it_and_clear_sample_keeps_real_rows(store):
    store.upsert_courses([course()])
    store.upsert_assignments([item()])
    real = store.save_material({"kind": "quiz", "title": "Mine", "body": quiz_body()})["id"]
    sample.load(store, now=NOW, timezone=ZONE)
    first = len(store.assignments())
    sample.load(store, now=NOW + timedelta(days=1).total_seconds(), timezone=ZONE)
    assert len(store.assignments()) == first and len(store.courses()) == 5
    assert store.material("sample:quiz")["best_score"] == pytest.approx(4 / 6)
    store.clear_sample()
    assert [c["id"] for c in store.courses()] == ["canvas:1"]
    assert [a["id"] for a in store.assignments()] == ["canvas:1:5"]
    assert [m["id"] for m in store.materials()] == [real]
    assert not store.has_sample()


def test_an_unknown_timezone_still_loads_the_sample(store):
    sample.load(store, now=NOW, timezone="Not/AZone")
    assert len(store.courses()) == 4


# --- converging imports (one row per course and per assignment) ----------------------

NAME = "Introduction to Programming"
KEY = "introduction to programming"


def dash_course(**values):
    """The Dashboard / all-grades page: name-hash id, no url."""
    return {"id": "canvas:9f8e7d6c5b4a", "name": NAME, "code": "CS 101", "grade": None, "score": None,
            "url": None, "source": "canvas", "sample": False, "merge_key": KEY} | values


def page_course(**values):
    """A course page: numeric id and its url."""
    return {"id": "canvas:12345", "name": NAME, "code": "CS 101", "grade": None, "score": None,
            "url": "https://school.instructure.com/courses/12345", "source": "canvas", "sample": False,
            "merge_key": KEY} | values


def dash_item(**values):
    return item("canvas:aaaaaaaaaaaa", course_id="canvas:9f8e7d6c5b4a", points=None, details="",
                merge_key=KEY + "|lab 5 lists") | values


def page_item(**values):
    return item("canvas:12345:bbbbbbbbbbbb", course_id="canvas:12345", title="Lab 5: Lists ",
                merge_key=KEY + "|lab 5 lists") | values


def one_of_each(store):
    courses, items = store.courses(), store.assignments()
    assert len(courses) == 1 and len(items) == 1, (courses, items)
    return courses[0], items[0]


def test_dashboard_then_course_page_converges_on_the_numeric_course(store):
    store.import_rows([dash_course(grade="B+", score=87.5)], [dash_item()])
    mid = store.save_material({"kind": "quiz", "title": "Q", "course_id": "canvas:9f8e7d6c5b4a",
                               "assignment_id": "canvas:aaaaaaaaaaaa", "body": quiz_body()})["id"]
    assert store.import_rows([page_course()], [page_item()]) == (1, 1)
    c, a = one_of_each(store)
    assert c["id"] == "canvas:12345" and c["merge_key"] == KEY
    assert c["grade"] == "B+" and c["score"] == 87.5 and c["url"].endswith("/courses/12345")
    assert a["id"] == "canvas:aaaaaaaaaaaa", "the id already stored is kept"
    assert a["course_id"] == "canvas:12345" and a["points"] == 20.0 and a["details"] == "Use lists."
    m = store.material(mid)
    assert m["course_id"] == "canvas:12345" and m["assignment_id"] == "canvas:aaaaaaaaaaaa"


def test_course_page_then_dashboard_keeps_the_numeric_course_and_its_data(store):
    store.import_rows([page_course(grade="B+")], [page_item(status="graded", score=18.0)])
    assert store.import_rows([dash_course(grade=None)], [dash_item()]) == (1, 1)
    c, a = one_of_each(store)
    assert c["id"] == "canvas:12345" and c["grade"] == "B+" and c["url"].endswith("/courses/12345")
    assert a["id"] == "canvas:12345:bbbbbbbbbbbb" and a["course_id"] == "canvas:12345"
    assert a["points"] == 20.0 and a["score"] == 18.0 and a["status"] == "graded"
    assert a["details"] == "Use lists."


def test_both_pages_in_one_import_and_re_imports_are_idempotent(store):
    for _ in range(3):
        assert store.import_rows([dash_course(), page_course()], [dash_item(), page_item()]) == (1, 1)
        c, a = one_of_each(store)
        assert c["id"] == "canvas:12345" and a["course_id"] == "canvas:12345" and a["points"] == 20.0
    for _ in range(2):
        store.import_rows([page_course()], [page_item()])
        store.import_rows([dash_course()], [dash_item()])
        assert one_of_each(store)[1]["id"] == a["id"]


def test_two_hash_ids_for_one_name_keep_the_existing_one(store):
    store.import_rows([dash_course()], [])
    store.import_rows([dash_course(id="web:1a1a1a1a1a1a", code=None)],
                      [dash_item(id="web:2b2b2b2b2b2b", course_id="web:1a1a1a1a1a1a")])
    c, a = one_of_each(store)
    assert c["id"] == "canvas:9f8e7d6c5b4a" and c["code"] == "CS 101" and a["course_id"] == c["id"]


def test_two_numeric_courses_with_one_name_stay_apart(store):
    store.import_rows([page_course()], [])
    store.import_rows([page_course(id="canvas:999", url=None)], [])
    assert sorted(c["id"] for c in store.courses()) == ["canvas:12345", "canvas:999"]


def test_pre_existing_duplicates_converge_on_the_next_import(store):
    # Rows written before converging existed: two course ids, two copies of one assignment.
    store.upsert_courses([dash_course(merge_key=None), page_course(merge_key=None)])
    store.upsert_assignments([dash_item(points=15.0), page_item(points=None)])
    store.import_rows([page_course()], [])
    c, a = one_of_each(store)
    assert c["id"] == "canvas:12345" and a["course_id"] == "canvas:12345"
    assert a["id"] == "canvas:12345:bbbbbbbbbbbb", "the richer row survives"
    assert a["details"] == "Use lists." and a["points"] == 15.0


def test_an_all_digit_name_hash_is_not_taken_for_a_numeric_id(store):
    store.import_rows([page_course()], [])
    store.import_rows([dash_course(id="canvas:123456789012")], [])
    assert [c["id"] for c in store.courses()] == ["canvas:12345"]


def test_an_old_database_gains_merge_key(tmp_path):
    import sqlite3
    path = tmp_path / "study" / "study.db"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE courses (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', code TEXT, grade TEXT,
            score REAL, url TEXT, source TEXT NOT NULL DEFAULT 'canvas', sample INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL);
        INSERT INTO courses(id, name, grade, updated_at) VALUES ('canvas:9f8e7d6c5b4a', 'Introduction  to
            Programming!', 'A', 1.0);
    """)
    db.close()
    old = StudyStore(path)
    assert old.courses()[0]["merge_key"] == KEY
    assert StudyStore(path).courses()[0]["grade"] == "A", "reopening is harmless"
    old.import_rows([page_course()], [])
    assert [(c["id"], c["grade"]) for c in old.courses()] == [("canvas:12345", "A")]


def test_real_pages_converge_whatever_the_order(tmp_path):
    """canvas_parse's own fixtures: the course page says "CS 101: Introduction to Programming",
    the Dashboard and all-grades pages only the name."""
    from service.study import canvas_parse as cp
    from tests import test_study_canvas_parse as pages
    parsed = {
        "dash": cp.parse_page("https://canvas.example.edu/", "Dashboard", pages.DASHBOARD,
                              now=pages.NOW, timezone=pages.TZ),
        "grades": cp.parse_page("https://canvas.example.edu/grades", "Grades", pages.ALL_GRADES,
                                now=pages.NOW, timezone=pages.TZ),
        "course": cp.parse_page("https://canvas.example.edu/courses/12345/grades",
                                "Grades for Jordan Example: CS 101: Introduction to Programming",
                                pages.COURSE_GRADES, now=pages.NOW, timezone=pages.TZ),
    }
    results = []
    for n, order in enumerate((("dash", "grades", "course"), ("course", "dash", "grades"),
                               ("grades", "course", "dash"))):
        store = StudyStore(tmp_path / str(n) / "study.db")
        for _ in range(2):   # re-importing changes nothing
            for name in order:
                store.import_rows(parsed[name].courses, parsed[name].assignments)
        python = [c for c in store.courses() if "introduction to programming" in c["merge_key"]]
        assert [c["id"] for c in python] == ["canvas:12345"], order
        assert python[0]["score"] == 87.5 and python[0]["url"].endswith("/courses/12345")
        rows = store.assignments()
        assert not {a["course_id"] for a in rows} - {c["id"] for c in store.courses()}, "no orphans"
        titles = [(a["course_id"], merge_key(a["title"])) for a in rows]
        assert len(titles) == len(set(titles)), "one row per (course, title)"
        results.append(sorted((c["id"], c["name"]) for c in store.courses()))
    assert results[0] == results[1] == results[2]


# --- a parser's default "upcoming" never undoes real status ---------------------------


@pytest.mark.parametrize("stored", ["submitted", "graded", "missing", "late"])
@pytest.mark.parametrize("path", ["upsert", "import"])
def test_a_re_import_never_marks_done_work_due_again(store, stored, path):
    write = (store.upsert_assignments if path == "upsert"
             else lambda rows: store.import_rows([course()], rows))
    write([item(status=stored, score=18.0 if stored == "graded" else None)])
    write([item(status="upcoming")])                     # same id, same deadline, no status shown
    row = store.assignments()[0]
    assert row["status"] == stored
    write([item(status="upcoming", due=NOW)])            # an EARLIER due time is no extension
    assert store.assignments()[0]["status"] == stored


@pytest.mark.parametrize("path", ["upsert", "import"])
def test_an_extended_deadline_makes_missing_work_upcoming_again(store, path):
    write = (store.upsert_assignments if path == "upsert"
             else lambda rows: store.import_rows([course()], rows))
    write([item(status="missing", due=NOW - 3600)])
    write([item(status="upcoming", due=NOW + 86400)])
    row = store.assignments()[0]
    assert row["status"] == "upcoming" and row["due_ts"] == NOW + 86400


def test_real_status_still_replaces_real_status(store):
    store.upsert_assignments([item(status="missing")])
    store.upsert_assignments([item(status="graded", score=17.0)])
    assert store.assignments()[0]["status"] == "graded"
    store.upsert_assignments([item(aid="canvas:1:6", title="Lab 6")])
    assert [a["status"] for a in store.assignments() if a["id"] == "canvas:1:6"] == ["upcoming"]


def test_a_twin_under_another_id_keeps_its_grade_on_the_merge_path(store):
    # The course page knew the grade under the numeric id; the Dashboard's name-keyed copy says upcoming.
    store.import_rows([course()], [item(status="graded", score=19.0)])
    store.import_rows([course()], [item(aid="canvas:abc123", status="upcoming", score=None)])
    rows = store.assignments()
    assert len(rows) == 1 and rows[0]["status"] == "graded" and rows[0]["score"] == 19.0


def test_folding_duplicates_keeps_the_later_deadline_over_an_old_missing(store):
    # Two rows of one course with one title fold into the richer one (the upcoming row with the
    # later deadline); the old row's "missing" belongs to the old deadline and is not carried over.
    store.upsert_courses([course()])
    store.upsert_assignments([item(aid="a-old", status="missing", due=NOW - 3600, details="", points=None),
                              item(aid="a-new", status="upcoming", due=NOW + 86400)])
    with store._lock:
        store._db.execute("BEGIN")
        store._dedupe_assignments("canvas:1", NOW)
        store._db.execute("COMMIT")
    rows = store.assignments()
    assert len(rows) == 1 and rows[0]["id"] == "a-new" and rows[0]["status"] == "upcoming"



def test_handed_in_work_is_never_reopened_by_a_later_looking_deadline(tmp_path):
    """A date-only page reads as 23:59, which looks like an extension; done work stays done."""
    store = StudyStore(tmp_path / "study.db")
    base = {"id": "canvas:1:lab", "course_id": "canvas:1", "title": "Lab 5", "kind": "assignment",
            "due_ts": 1000.0, "source": "canvas"}
    for done in ("submitted", "graded"):
        store.upsert_assignments([{**base, "status": done}])
        store.upsert_assignments([{**base, "status": "upcoming", "due_ts": 90000.0}])
        assert store.assignments()[0]["status"] == done
    # Missing work with a real extension is due again.
    store.upsert_assignments([{**base, "id": "canvas:1:hw", "title": "HW 2", "status": "missing"}])
    store.upsert_assignments([{**base, "id": "canvas:1:hw", "title": "HW 2", "status": "upcoming", "due_ts": 90000.0}])
    assert next(a for a in store.assignments() if a["id"] == "canvas:1:hw")["status"] == "upcoming"


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_study_store.py"' in manifest
