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


# --- sources ---------------------------------------------------------------------------

SUMMARY_KEYS = {"id", "course_id", "kind", "title", "url", "chars", "created_at", "pages", "deck_status",
                "week", "topic", "summary"}


def drive(url="https://docs.google.com/document/d/abc", **values):
    return {"kind": "drive", "url": url, "title": "Unit 3 notes", "text": "Mitosis has four phases.",
            "course_id": "canvas:1"} | values


def test_a_source_is_saved_listed_read_and_deleted(store):
    saved = store.save_source(drive())
    assert set(saved) == SUMMARY_KEYS and saved["id"].startswith("src_") and len(saved["id"]) == 16
    assert saved["chars"] == len("Mitosis has four phases.") and saved["kind"] == "drive"
    assert store.sources() == [saved] and store.sources("canvas:1") == [saved] and store.sources("x") == []
    full = store.source(saved["id"])
    assert full == saved | {"text": "Mitosis has four phases."}
    assert store.delete_source(saved["id"]) is True and store.delete_source(saved["id"]) is False
    assert store.source(saved["id"]) is None and store.sources() == []


def test_the_same_kind_and_url_replaces_text_title_and_course_but_keeps_the_id(store):
    first = store.save_source(drive())
    again = store.save_source(drive(title="Unit 3 (new)", text="Five phases now.", course_id=None))
    assert again["id"] == first["id"] and again["course_id"] is None and again["title"] == "Unit 3 (new)"
    assert store.source(first["id"])["text"] == "Five phases now." and len(store.sources()) == 1
    page = store.save_source(drive(kind="page"))   # another kind is another source
    assert page["id"] != first["id"] and len(store.sources()) == 2


def test_sources_are_newest_first_capped_and_checked(store):
    import time as _time
    older = store.save_source(drive("https://docs.google.com/document/d/one"))
    _time.sleep(0.002)
    newer = store.save_source(drive("https://docs.google.com/document/d/two", text="x" * 500_000))
    assert [s["id"] for s in store.sources()] == [newer["id"], older["id"]]
    from service.study.store import SOURCE_CHARS
    assert newer["chars"] == SOURCE_CHARS == 400_000 and len(store.source(newer["id"])["text"]) == SOURCE_CHARS
    with pytest.raises(ValueError):
        store.save_source(drive(kind="email"))


def test_clear_drops_sources_and_clear_sample_keeps_them(store):
    sample.load(store, now=NOW, timezone=ZONE)
    saved = store.save_source(drive())
    store.clear_sample()
    assert [s["id"] for s in store.sources()] == [saved["id"]]
    store.clear()
    assert store.sources() == []


def test_a_merged_course_carries_its_sources(store):
    store.import_rows([dash_course()], [])
    hashed = store.courses()[0]["id"]
    saved = store.save_source(drive(course_id=hashed))
    store.import_rows([page_course()], [])
    assert store.source(saved["id"])["course_id"] == "canvas:12345"


def test_an_old_database_gains_the_sources_table(tmp_path):
    import sqlite3
    path = tmp_path / "study" / "study.db"
    path.parent.mkdir()
    old = StudyStore(path)
    old.upsert_courses([course()])
    old._db.execute("DROP TABLE sources")   # as a study.db from before sources
    old._db.close()
    db = sqlite3.connect(path)
    assert "sources" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    reopened = StudyStore(path)
    assert [c["id"] for c in reopened.courses()] == ["canvas:1"] and reopened.sources() == []
    assert reopened.save_source(drive())["kind"] == "drive"


def test_find_materials_kinds_labels_and_reading_status(store):
    for kind in ("slides", "doc", "pdf", "notebook", "article", "page", "discussion", "file", "video", "other"):
        store.save_source({"kind": kind, "url": f"https://example.edu/{kind}", "title": kind, "text": "t"})
    with pytest.raises(ValueError):
        store.save_source({"kind": "podcast", "url": "https://example.edu/p", "title": "p", "text": "t"})
    deck = next(s for s in store.sources() if s["kind"] == "slides")
    assert (deck["week"], deck["topic"], deck["summary"]) == (None, None, None)
    store.set_deck_status(deck["id"], "reading")
    assert store.set_labels(deck["id"], week=5, topic="  Sampling   bias ", summary="Why samples mislead.")
    got = store.source(deck["id"])
    assert (got["deck_status"], got["week"], got["topic"], got["summary"]) == (
        "reading", 5, "Sampling bias", "Why samples mislead.")
    store.set_labels(deck["id"], week=True, topic=3, summary="x" * 999)
    got = store.source(deck["id"])
    assert got["week"] is None and got["topic"] is None and len(got["summary"]) == 300
    assert not store.set_labels("src_missing", week=1)


def test_resaving_by_id_keeps_the_row_and_its_labels(store):
    old = store.save_source({"kind": "drive", "url": "https://docs.google.com/presentation/d/AbCdEfGhIjK1/edit",
                             "title": "Lecture 1", "text": "old"})
    store.set_labels(old["id"], week=1, topic="Intro", summary="The first lecture.")
    store.save_pages(old["id"], [{"page": 1, "title": "Hi", "points": ["a"], "figure": None, "topics": ["x"]}])
    new = store.save_source({"id": old["id"], "kind": "slides", "url": "https://docs.google.com/presentation/d/"
                             "AbCdEfGhIjK1", "title": "Lecture 1", "text": "new", "course_id": "canvas:7"})
    assert new["id"] == old["id"] and new["kind"] == "slides" and new["week"] == 1 and new["pages"] == 1
    assert len(store.sources()) == 1 and store.source(old["id"])["text"] == "new"
    store.set_source_text(old["id"], "from pages")
    assert store.source(old["id"])["chars"] == len("from pages")


def test_an_old_sources_table_gains_the_label_columns(tmp_path):
    import sqlite3
    path = tmp_path / "study" / "study.db"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, course_id TEXT, kind TEXT NOT NULL, title TEXT NOT NULL"
               " DEFAULT '', url TEXT, text TEXT NOT NULL DEFAULT '', chars INTEGER NOT NULL DEFAULT 0,"
               " created_at REAL NOT NULL, deck_status TEXT NOT NULL DEFAULT 'none', UNIQUE (kind, url))")
    db.execute("INSERT INTO sources VALUES ('src_old', NULL, 'drive', 'Old', 'u', 't', 1, 1.0, 'ready')")
    db.commit()
    db.close()
    got = StudyStore(path).sources()[0]
    assert got["deck_status"] == "ready" and (got["week"], got["topic"], got["summary"]) == (None, None, None)
    StudyStore(path)   # opening twice is fine


def test_remove_school_data_clears_pages_misses_and_labels(store):
    src = store.save_source({"kind": "slides", "url": "https://docs.google.com/presentation/d/AbCdEfGhIjK2",
                             "title": "Deck", "text": "t"})
    store.save_pages(src["id"], [{"page": 1, "title": "Hi", "points": ["a"], "figure": None, "topics": ["x"]}])
    store.set_labels(src["id"], week=2, topic="T", summary="S")
    quiz = store.save_material({"kind": "quiz", "title": "Q", "body": {"questions": []}})
    store.record_misses(quiz["id"], [{"question": 0, "source_id": src["id"], "page": 1}])
    other = store.save_source({"kind": "article", "url": "https://news.example.org/a", "title": "A", "text": "t"})
    store.save_pages(other["id"], [{"page": 1, "title": "Hi", "points": ["a"], "figure": None, "topics": ["x"]}])
    assert store.delete_source(other["id"]) and store.pages(other["id"]) == []
    store.clear()
    assert store.sources() == [] and store.pages(src["id"]) == [] and store.misses() == []
    assert store.search_pages("hi") == []


# --- a nameless course page meets the Dashboard's named row ---------------------------

def named(cid="canvas:7d2e9a4c61f8", name="Statistics", **values):
    """The Dashboard / all-grades row: name-hash id, no url."""
    return {"id": cid, "name": name, "code": None, "grade": None, "score": 92.5, "url": None,
            "source": "canvas", "sample": False} | values


def nameless(cid="canvas:12345", **values):
    """A course page that showed no course name: numeric id and url, empty name."""
    return {"id": cid, "name": "", "code": None, "grade": None, "score": 92.5,
            "url": f"https://canvas.example.edu/courses/{cid.split(':')[1]}", "source": "canvas",
            "sample": False, "merge_key": ""} | values


def dash_hw(course_id="canvas:7d2e9a4c61f8"):
    return item("canvas:dddddddddddd", course_id=course_id, title="HW2", url=None, points=None, details="")


def page_hw(course_id="canvas:12345"):
    return item("canvas:12345:eeeeeeeeeeee", course_id=course_id, title="HW2",
                url="https://canvas.example.edu/courses/12345/assignments")


def test_a_nameless_course_page_merges_into_the_one_named_row_with_its_score(store):
    store.import_rows([named(), named("canvas:a1c4e7f20b93", "Introduction to Programming", score=80.0)],
                      [dash_hw()])
    mid = store.save_material({"kind": "quiz", "title": "Q", "course_id": "canvas:7d2e9a4c61f8",
                               "body": quiz_body()})["id"]
    sid = store.save_source({"kind": "page", "url": "https://canvas.example.edu/", "title": "Dashboard",
                             "text": "x", "course_id": "canvas:7d2e9a4c61f8"})["id"]
    store.import_rows([nameless()], [page_hw()])
    rows = {c["id"]: c for c in store.courses()}
    assert set(rows) == {"canvas:12345", "canvas:a1c4e7f20b93"}
    c = rows["canvas:12345"]
    assert c["name"] == "Statistics" and c["merge_key"] == "statistics"
    assert c["url"] == "https://canvas.example.edu/courses/12345" and c["score"] == 92.5
    items = store.assignments()
    assert len(items) == 1 and items[0]["course_id"] == "canvas:12345"
    assert store.material(mid)["course_id"] == "canvas:12345"
    assert store.source(sid)["course_id"] == "canvas:12345"
    # A later Dashboard read finds the row by its name: no split.
    store.import_rows([named(score=93.0)], [dash_hw()])
    assert {c["id"] for c in store.courses()} == {"canvas:12345", "canvas:a1c4e7f20b93"}
    c = {c["id"]: c for c in store.courses()}["canvas:12345"]
    assert c["score"] == 93.0 and c["url"].endswith("/courses/12345") and c["name"] == "Statistics"
    assert [a["course_id"] for a in store.assignments()] == ["canvas:12345"]
    # And a later nameless read of the course page keeps the name.
    store.import_rows([nameless(score=93.0)], [page_hw()])
    assert {c["id"]: c for c in store.courses()}["canvas:12345"]["name"] == "Statistics"


def test_the_course_page_first_then_the_dashboard_also_merges(store):
    store.import_rows([nameless()], [page_hw()])
    store.import_rows([named()], [dash_hw()])
    (c,) = store.courses()
    assert c["id"] == "canvas:12345" and c["name"] == "Statistics" and c["url"].endswith("/courses/12345")
    assert [a["course_id"] for a in store.assignments()] == ["canvas:12345"]


@pytest.mark.parametrize("rows", [
    [named(), named("canvas:a1c4e7f20b93", "Introduction to Programming")],   # two share the score
    [named(score=91.0)],                                                       # scores differ
    [named(url="https://canvas.example.edu/courses/999")],                      # already has an address
    [named(sample=True)],                                                      # a sample row
    [named(score=None)],                                                       # no grade to match on
    [named(code="CS 101")],                                                    # codes disagree
    [named(grade="A-")],                                                       # letters disagree
])
def test_no_merge_without_one_clear_match(store, rows):
    store.upsert_courses(rows)
    store.import_rows([nameless(code="STAT 101", grade="A")], [])
    assert "canvas:12345" in {c["id"] for c in store.courses()}
    assert {c["id"]: c for c in store.courses()}["canvas:12345"]["name"] == ""
    assert len(store.courses()) == len(rows) + 1


def test_two_nameless_rows_with_one_score_merge_neither(store):
    store.import_rows([named(), nameless(), nameless("canvas:12346")], [])
    assert len(store.courses()) == 3


def test_opening_the_store_heals_an_already_split_store_once(tmp_path):
    path = tmp_path / "study" / "study.db"
    old = StudyStore(path)
    # The split as an earlier build left it: plain upserts, no import-time merge.
    old.upsert_courses([named(), named("canvas:a1c4e7f20b93", "Introduction to Programming", score=80.0),
                        nameless()])
    old.upsert_assignments([dash_hw(), page_hw()])
    old._db.close()
    healed = StudyStore(path)
    rows = {c["id"]: c for c in healed.courses()}
    assert set(rows) == {"canvas:12345", "canvas:a1c4e7f20b93"}
    assert rows["canvas:12345"]["name"] == "Statistics" and rows["canvas:12345"]["url"].endswith("/12345")
    items = healed.assignments()
    assert len(items) == 1 and items[0]["course_id"] == "canvas:12345", "the twin assignment folded in"
    before = (healed.courses(), healed.assignments())
    healed._db.close()
    again = StudyStore(path)
    assert (again.courses(), again.assignments()) == before


# --- one course under a coded, sectioned course-page name and a plain Dashboard name ----------

WRIT = "Writing and Rhetoric: Voices and Public Argument"
WRIT_PAGE = "WRIT 1 Writing and Rhetoric: Voices & Public Argument Section 04"
WRIT_DUE = datetime(2026, 11, 18, 23, 59, tzinfo=ZoneInfo(ZONE)).timestamp()


def writ_dash(cid="canvas:5e2a9c41d07b", name=WRIT, **values):
    return {"id": cid, "name": name, "code": None, "grade": None, "score": None, "url": None,
            "source": "canvas", "sample": False} | values


def writ_page(cid="canvas:40236", name=WRIT_PAGE, code="WRIT-1-04", **values):
    return {"id": cid, "name": name, "code": code, "grade": None, "score": None,
            "url": f"https://canvas.example.edu/courses/{cid.split(':')[1]}", "source": "canvas",
            "sample": False} | values


PAGE_TITLES = ("Essay 1 Draft", "Reading Response 2", "Peer Review", "Essay 1 Final", "Reflection 3",
               "Portfolio")


def writ_items(course_id, prefix, titles=("Essay 1 Draft",), due=WRIT_DUE):
    """Each title is due on its own day (the same day whichever course lists it)."""
    return [item(f"{prefix}{n:02d}", course_id=course_id, title=title, url=None, details="",
                 due=due + (PAGE_TITLES.index(title) if title in PAGE_TITLES else 20) * 86400)
            for n, title in enumerate(titles)]


def writ_page_items(course_id="canvas:40236"):
    return writ_items(course_id, f"{course_id}:", PAGE_TITLES)


def writ_dash_items(course_id="canvas:5e2a9c41d07b", titles=("Essay 1 Draft",)):
    return writ_items(course_id, "canvas:dash", titles)


def assert_one_writ(store):
    (c,) = store.courses()
    assert c["id"] == "canvas:40236" and c["url"] == "https://canvas.example.edu/courses/40236"
    assert c["name"] == WRIT and c["code"] == "WRIT-1-04" and c["merge_key"] == merge_key(WRIT)
    items = store.assignments()
    assert len(items) == 6 and {a["course_id"] for a in items} == {"canvas:40236"}
    return c


@pytest.mark.parametrize("order", ["dash_first", "page_first", "together"])
def test_a_coded_sectioned_course_page_name_merges_with_the_dashboard_name(store, order):
    dash = ([writ_dash()], writ_dash_items())
    page = ([writ_page()], writ_page_items())
    if order == "together":
        store.import_rows(dash[0] + page[0], dash[1] + page[1])
    else:
        for courses, items in ((dash, page) if order == "dash_first" else (page, dash)):
            store.import_rows(courses, items)
    assert_one_writ(store)
    # Later reads of either page neither split the course nor rename it.
    for _ in range(2):
        store.import_rows([writ_page()], writ_page_items())
        assert_one_writ(store)
        store.import_rows([writ_dash()], writ_dash_items())
        assert_one_writ(store)


def test_the_split_store_heals_at_open_once_with_materials_and_sources_moved(tmp_path):
    path = tmp_path / "study" / "study.db"
    old = StudyStore(path)
    old.upsert_courses([writ_dash(), writ_page()])
    old.upsert_assignments(writ_dash_items() + writ_page_items())
    mid = old.save_material({"kind": "quiz", "title": "Q", "course_id": "canvas:5e2a9c41d07b",
                             "assignment_id": "canvas:dash00", "body": quiz_body()})["id"]
    sid = old.save_source({"kind": "page", "url": "https://canvas.example.edu/", "title": "Dashboard",
                           "text": "x", "course_id": "canvas:5e2a9c41d07b"})["id"]
    old._db.close()
    healed = StudyStore(path)
    assert_one_writ(healed)
    m = healed.material(mid)
    assert m["course_id"] == "canvas:40236" and m["assignment_id"] in {a["id"] for a in healed.assignments()}
    assert healed.source(sid)["course_id"] == "canvas:40236"
    before = (healed.courses(), healed.assignments())
    healed._db.close()
    again = StudyStore(path)
    assert (again.courses(), again.assignments()) == before


@pytest.mark.parametrize("page_name,code", [
    ("WRIT 1 Writing and Rhetoric: Voices & Public Argument", "WRIT 1"),       # & for and
    ("WRIT 1: Writing and Rhetoric: Voices and Public Argument", None),         # code and colon
    ("WRIT-1 - Writing and Rhetoric: Voices and Public Argument", None),        # code and dash
    ("WRIT 1 Writing and Rhetoric: Voices and Public Argument Sec 04", "WRIT-1-04"),
    ("WRIT 1 Writing and Rhetoric: Voices and Public Argument - 04", "WRIT-1-04"),
    ("WRIT 1 Writing and Rhetoric: Voices and Public Argument (04)", "WRIT-1-04"),
    ("WRIT 1 Writing and Rhetoric: Voices and Public Argument 04", "WRIT-1-04"),
    ("WRIT-1-04 Writing and Rhetoric: Voices and Public Argument", None),
])
def test_name_forms_that_name_one_course(store, page_name, code):
    store.import_rows([writ_dash()], [])
    store.import_rows([writ_page(name=page_name, code=code)], [])
    (c,) = store.courses()
    assert c["id"] == "canvas:40236" and c["name"] == WRIT


def test_a_bare_number_is_only_a_section_when_the_code_says_so(store):
    store.import_rows([writ_dash(name="Writing Studio")], [])
    store.import_rows([writ_page(name="WRIT 1 Writing Studio 04", code="WRIT 1")], [])
    assert len(store.courses()) == 2


@pytest.mark.parametrize("first,second", [
    # The same title under two subject+number codes.
    (writ_page("canvas:40236", "WRIT 1 Writing and Rhetoric", "WRIT-1-04"),
     writ_dash(name="WRIT 2 Writing and Rhetoric", code="WRIT 2")),
    (writ_dash(name="WRIT 1: Writing and Rhetoric"), writ_dash("canvas:6f3b0d52e18c", "WRIT 2: Writing and Rhetoric")),
    # Two coded rows of one course in different sections.
    (writ_dash(name="WRIT 1 Writing and Rhetoric Section 04", code="WRIT-1-04"),
     writ_page(name="WRIT 1 Writing and Rhetoric Section 10", code="WRIT-1-10")),
    # What is left after the code is too short to tell courses apart.
    (writ_dash(name="Seminar"), writ_page(name="WRIT 1 Seminar", code="WRIT 1")),
])
def test_names_that_stay_two_courses(store, first, second):
    store.import_rows([first], [])
    store.import_rows([second], [])
    assert len(store.courses()) == 2


def test_two_course_pages_with_one_clean_name_stay_apart(store):
    store.import_rows([writ_page()], [])
    store.import_rows([writ_page("canvas:95237", name=WRIT, code=None)], [])
    assert sorted(c["id"] for c in store.courses()) == ["canvas:40236", "canvas:95237"]


def test_a_dashboard_row_without_a_code_joins_a_sectioned_course(store):
    store.import_rows([writ_page(name="WRIT 1 Writing and Rhetoric Section 04")], [])
    store.import_rows([writ_dash(name="Writing and Rhetoric")], [])
    (c,) = store.courses()
    assert c["id"] == "canvas:40236" and c["name"] == "Writing and Rhetoric" and c["code"] == "WRIT-1-04"


def test_course_name_keys():
    from service.study.course_names import course_key, same_course
    a = course_key(WRIT_PAGE, "WRIT-1-04")
    assert a.course == "writ 1" and a.section == "04" and not a.clean
    assert merge_key(WRIT) in a.keys
    b = course_key(WRIT)
    assert b.clean and same_course(a, b)
    assert not same_course(course_key("A + B Studies"), course_key("A and B Studies")), "only & is and"
    assert course_key("Seminar Section 2").keys == {"seminar section 2"}, "too short once stripped"
    assert course_key("Writing (Fall 2026)").keys == {"writing fall 2026"}, "terms are left alone"


# --- the assignment evidence: a Dashboard name that still matches no course page ----------

OTHER = "Global Voices Writing Seminar"   # the Dashboard's name for the course, unlike the page's


def evidence_store(store, dash_titles=("Essay 1 Draft",), **dash):
    store.import_rows([writ_page()], writ_page_items())
    store.import_rows([writ_dash(name=OTHER) | dash], writ_dash_items(titles=dash_titles))
    return store


def test_all_dated_dashboard_items_in_one_course_page_merge_them(store):
    evidence_store(store, dash_titles=("Essay 1 Draft", "Reading Response 2"))
    (c,) = store.courses()
    assert c["id"] == "canvas:40236" and c["name"] == OTHER and c["code"] == "WRIT-1-04"
    assert len(store.assignments()) == 6
    # A later read of either page does not split or rename it.
    store.import_rows([writ_page()], writ_page_items())
    store.import_rows([writ_dash(name=OTHER)], writ_dash_items(titles=("Essay 1 Draft", "Reading Response 2")))
    (c,) = store.courses()
    assert c["id"] == "canvas:40236" and c["name"] == OTHER and len(store.assignments()) == 6


def test_only_some_dated_items_matching_is_not_enough(store):
    evidence_store(store, dash_titles=("Essay 1 Draft", "Lab Notebook"))
    assert len(store.courses()) == 2


def test_two_course_pages_holding_the_items_merge_neither(store):
    store.import_rows([writ_page("canvas:95237", name="WRIT 2 Argument and Research", code="WRIT 2")],
                      writ_page_items("canvas:95237"))
    evidence_store(store)
    assert len(store.courses()) == 3


def test_a_course_page_that_took_a_dashboard_name_takes_no_second_one(store):
    evidence_store(store)
    store.import_rows([writ_dash("canvas:6f3b0d52e18c", "Another Dashboard Name")],
                      writ_items("canvas:6f3b0d52e18c", "canvas:dosh", ("Peer Review",)))
    # The course now shows OTHER: a second, different plain name is another course.
    assert sorted(c["name"] for c in store.courses()) == ["Another Dashboard Name", OTHER]


def test_two_dashboard_rows_held_by_one_course_page_at_once_merge_neither(store):
    store.import_rows([writ_page(), writ_dash(name=OTHER), writ_dash("canvas:6f3b0d52e18c", "Another Name")],
                      writ_page_items() + writ_dash_items()
                      + writ_items("canvas:6f3b0d52e18c", "canvas:dosh", ("Peer Review",)))
    assert len(store.courses()) == 3


def test_undated_dashboard_items_are_no_evidence(store):
    store.import_rows([writ_page()], writ_page_items())
    store.import_rows([writ_dash(name=OTHER)], [i | {"due_ts": None} for i in writ_dash_items()])
    assert len(store.courses()) == 2


def test_another_due_day_is_no_evidence(store):
    store.import_rows([writ_page()], writ_page_items())
    store.import_rows([writ_dash(name=OTHER)], writ_items("canvas:5e2a9c41d07b", "canvas:dash",
                                                           due=WRIT_DUE + 86400))
    assert len(store.courses()) == 2


@pytest.mark.parametrize("page,dash", [
    ({"score": 91.0}, {"score": 84.0}),
    ({}, {"code": "HIST 10"}),
    ({"grade": "A"}, {"grade": "B"}),
    ({}, {"url": "https://canvas.example.edu/courses/77"}),
])
def test_conflicting_evidence_keeps_them_apart(store, page, dash):
    store.import_rows([writ_page() | page], writ_page_items())
    store.import_rows([writ_dash(name=OTHER) | dash], writ_dash_items())
    assert len(store.courses()) == 2


def test_the_evidence_rule_also_heals_at_open(tmp_path):
    path = tmp_path / "study" / "study.db"
    old = StudyStore(path)
    old.upsert_courses([writ_dash(name=OTHER), writ_page()])
    old.upsert_assignments(writ_dash_items() + writ_page_items())
    sid = old.save_source({"kind": "page", "url": "https://canvas.example.edu/", "title": "Dashboard",
                           "text": "x", "course_id": "canvas:5e2a9c41d07b"})["id"]
    old._db.close()
    healed = StudyStore(path)
    (c,) = healed.courses()
    assert c["id"] == "canvas:40236" and c["name"] == OTHER
    items = healed.assignments()
    assert len(items) == 6 and {a["course_id"] for a in items} == {"canvas:40236"}, "twins folded, none orphaned"
    assert healed.source(sid)["course_id"] == "canvas:40236"


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_study_store.py"' in manifest
