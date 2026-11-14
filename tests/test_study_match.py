"""service/study/match.py and ready.py, and the slides parts of day_hook/plan: which slides cover
an assignment, the prepared quiz (consent and cost bounds), and the today object's
"ready"/"weak" items. Made-up course data only; the store's page methods are faked."""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from service.study import day_hook, match, ready
from service.study.store import StudyStore

TZ = "America/Los_Angeles"
ZONE = ZoneInfo(TZ)
DAY = "2026-11-16"   # a Monday
QUIZ = {"questions": [{"prompt": f"Q{i}", "choices": ["a", "b", "c", "d"], "answer": 0, "why": "w",
                       "slide": None} for i in range(10)]}


def at(h, mi=0, d=16):
    return datetime(2026, 11, d, h, mi, tzinfo=ZONE).timestamp()


class PagesStore(StudyStore):
    """The real store plus the contract's page and miss methods (hs2-ingest builds the real ones)."""

    def __init__(self, path):
        super().__init__(path)
        self._pages: dict[str, list[dict]] = {}
        self._misses: list[dict] = []

    def save_pages(self, source_id, pages):
        self._pages[source_id] = [dict(p) for p in pages]
        return len(pages)

    def pages(self, source_id):
        return [dict(p) for p in self._pages.get(source_id, [])]

    def sources(self, course_id=None):
        return [{**s, "pages": len(self._pages.get(s["id"], [])),
                 "deck_status": "ready" if self._pages.get(s["id"]) else "none"}
                for s in super().sources(course_id)]

    def record_misses(self, material_id, rows):
        for r in rows:
            self._misses.append({"material_id": material_id, **r, "at": time.time()})
        return len(rows)

    def misses(self, course_id=None, since=None):
        mats = {m["id"]: m.get("course_id") for m in self.materials()}
        return [m for m in self._misses if (course_id is None or mats.get(m["material_id"]) == course_id)
                and (since is None or m["at"] >= since)]


def page(n, title, points=(), topics=(), figure=None):
    return {"page": n, "title": title, "points": list(points), "figure": figure, "topics": list(topics)}


@pytest.fixture
def store(tmp_path):
    s = PagesStore(tmp_path / "study" / "study.db")
    s.upsert_courses([{"id": "canvas:7", "name": "Data Structures", "code": "CSE 42", "grade": None,
                       "score": None, "url": None, "source": "canvas", "sample": False},
                      {"id": "canvas:8", "name": "Plant Biology", "code": "BIO 3", "grade": None,
                       "score": None, "url": None, "source": "canvas", "sample": False}])
    return s


def deck(store, title, pages, course_id="canvas:7"):
    src = store.save_source({"kind": "drive", "url": f"https://drive.example/{title}", "title": title,
                             "text": "", "course_id": course_id})
    store.save_pages(src["id"], pages)
    return src["id"]


def task(aid, title, due, kind="assignment", course_id="canvas:7", details="", status="upcoming"):
    return {"id": aid, "course_id": course_id, "title": title, "kind": kind, "due_ts": due, "all_day": False,
            "points": 10.0, "score": None, "status": status, "details": details, "url": None,
            "source": "canvas", "sample": False}


# ---------------------------------------------------------------- refs and phrases

@pytest.mark.parametrize("text,want", [
    ("Read Lecture 7", {("lecture", 7)}),
    ("Lectures 7 and 8", {("lecture", 7), ("lecture", 8)}),
    ("lec 3, 4 & 6", {("lecture", 3), ("lecture", 4), ("lecture", 6)}),
    ("Weeks 5-7 review", {("week", 5), ("week", 6), ("week", 7)}),
    ("CSE42_Week5.pdf", {("week", 5)}),
    ("L09 Hash tables", {("lecture", 9)}),
    ("Html5 and Problem Set 3", set()),
])
def test_refs(text, want):
    assert match.refs(text) == want


def test_covers_text_phrases():
    whole = lambda sid, d: {"source_id": sid, "deck": d, "pages": [1, 2], "whole": True,  # noqa: E731
                            "label": match.deck_label(d), "hits": []}
    assert match.covers_text({}, [whole("a", "Lecture 7: Trees"), whole("b", "Lecture 8: Heaps")]) == \
        "Lectures 7 and 8 cover it."
    assert match.covers_text({}, [whole("a", "Lecture 7: Trees")]) == "Lecture 7 covers it."
    part = {"source_id": "a", "deck": "Lecture 7: Trees", "pages": [4, 5, 6, 7, 8, 9], "whole": False,
            "label": "Lecture 7", "hits": [4]}
    assert match.covers_text({}, [part]) == "Slides 4–9 of Lecture 7 cover it."
    assert match.covers_text({}, [{**part, "pages": [4]}]) == "Slide 4 of Lecture 7 covers it."
    assert match.covers_text({}, []) is None
    assert match.from_phrase([whole("a", "Week 5"), whole("b", "Week 6")]) == "weeks 5 and 6"


# ---------------------------------------------------------------- match

def test_match_by_lecture_number_and_by_words(store):
    l7 = deck(store, "Lecture 7: Binary trees", [page(1, "Trees"), page(2, "Traversal", ["inorder walk"])])
    l8 = deck(store, "Lecture 8: Heaps", [page(1, "Heaps"), page(2, "Heap sort")])
    deck(store, "Lecture 9: Graphs", [page(1, "Graphs", topics=["graphs"])])
    deck(store, "Lecture 7: Photosynthesis", [page(1, "Light")], course_id="canvas:8")   # other course
    got = match.match(task("pa3", "PA 3: Priority queues", at(17, d=20),
                           details="Uses material from Lectures 7 and 8."), store)
    assert len(got) == 2
    assert {m["source_id"] for m in got} == {l7, l8}
    assert all(m["whole"] and m["pages"] == [1, 2] for m in got)
    assert match.covers_text({}, got) == "Lectures 7 and 8 cover it."

    words = match.match(task("q", "Quiz: graphs and cycles", at(10, d=19), kind="quiz"), store)
    assert words[0]["deck"] == "Lecture 9: Graphs" and words[0]["whole"] is False


def test_match_words_pick_pages_inside_one_deck(store):
    sid = deck(store, "Week 4 slides", [page(1, "Agenda"), page(2, "Pointer basics", ["address of"], ["pointers"]),
                                        page(3, "Pointer arithmetic", topics=["pointers"]), page(4, "Recap")])
    got = match.match(task("hw", "HW 5 on pointers", at(23, d=18)), store)
    assert got == [{"source_id": sid, "deck": "Week 4 slides", "course_id": "canvas:7", "pages": [2, 3],
                    "score": got[0]["score"], "label": "Week 4", "whole": False, "hits": [2, 3]}]
    assert match.covers_text({}, got) == "Slides 2–3 of Week 4 cover it."
    assert match.review_pages(got) == [{"source_id": sid, "page": 2}, {"source_id": sid, "page": 3}]


def test_match_without_pages_or_with_a_store_that_has_none(store):
    assert match.match(task("x", "Essay", at(9)), store) == []
    assert match.match(task("x", "Essay", at(9)), object()) == []
    assert match.match(None, store) == []


# ---------------------------------------------------------------- ready.prepare

def _quiz_setup(store, n=1):
    sids = [deck(store, "Week 5: Hashing", [page(i, f"Hashing {i}", topics=["hashing"]) for i in (1, 2, 3)]),
            deck(store, "Week 6: Collisions", [page(1, "Probing")])]
    rows = [task(f"q{i}", f"Quiz {i}: weeks 5 and 6", time.time() + (i + 1) * 3600, kind="quiz") for i in range(n)]
    store.upsert_assignments(rows)
    return sids


class Maker:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def __call__(self, kind, **kw):
        self.calls.append({"kind": kind, **kw})
        if self.fail:
            raise RuntimeError("model down")
        return {"title": "Hashing quiz", "body": QUIZ}


READY = {"ready": True, "active_model_id": "made-up-model"}


def test_prepare_needs_consent_and_a_ready_connector(store):
    _quiz_setup(store)
    maker = Maker()
    assert asyncio.run(ready.prepare(store=store, status=READY, make=maker)) == []
    store.set_meta("auto_prepare", True)
    assert asyncio.run(ready.prepare(store=store, status={"ready": False}, make=maker)) == []
    assert maker.calls == []
    made = asyncio.run(ready.prepare(store=store, status=READY, make=maker))
    assert len(made) == 1 and maker.calls[0]["count"] == 10 and maker.calls[0]["kind"] == "quiz"
    pages = maker.calls[0]["pages"]
    assert [p["deck"] for p in pages][:3] == ["Week 5: Hashing"] * 3 and len(pages) == 4
    saved = store.material(made[0])
    assert saved["assignment_id"] == "q0" and saved["model"] == "made-up-model"
    # Once per assignment.
    assert asyncio.run(ready.prepare(store=store, status=READY, make=maker)) == []
    assert len(maker.calls) == 1


def test_prepare_caps_at_three_a_day_and_records_failures(store):
    _quiz_setup(store, n=5)
    store.set_meta("auto_prepare", True)
    maker = Maker(fail=True)
    assert asyncio.run(ready.prepare(store=store, status=READY, make=maker)) == []
    assert len(maker.calls) == 3                       # a failure still counts against the day
    assert asyncio.run(ready.prepare(store=store, status=READY, make=Maker())) == []
    later = asyncio.run(ready.prepare(time.time() + 86400 + 60, store=store, status=READY, make=Maker()))
    assert later == []   # the other two are past due by then: nothing left in the 48 h window


def test_prepare_skips_far_and_done_tests_and_ones_with_a_quiz(store):
    _quiz_setup(store)
    store.upsert_assignments([task("far", "Quiz far", time.time() + 5 * 86400, kind="quiz"),
                              task("done", "Quiz done", time.time() + 3600, kind="quiz", status="submitted")])
    store.save_material({"kind": "quiz", "title": "Mine", "course_id": "canvas:7", "assignment_id": "q0",
                         "body": QUIZ})
    store.set_meta("auto_prepare", True)
    maker = Maker()
    assert asyncio.run(ready.prepare(store=store, status=READY, make=maker)) == []


def test_the_next_quiz_leans_on_missed_pages(store):
    sids = _quiz_setup(store)
    old = store.save_material({"kind": "quiz", "title": "Old", "course_id": "canvas:7", "body": QUIZ})
    store.record_misses(old["id"], [{"question": 0, "source_id": sids[1], "page": 1, "topic": "probing"}])
    store.set_meta("auto_prepare", True)
    maker = Maker()
    asyncio.run(ready.prepare(store=store, status=READY, make=maker))
    first = maker.calls[0]["pages"][0]
    assert (first["source_id"], first["page"]) == (sids[1], 1)


def test_kick_never_runs_twice_at_once(store, monkeypatch):
    store.set_meta("auto_prepare", True)
    gate, runs = threading.Event(), []

    async def slow(now=None, *, store=None, **_):
        runs.append(now)
        await asyncio.to_thread(gate.wait, 5)
        return []
    monkeypatch.setattr(ready, "prepare", slow)
    monkeypatch.setattr(ready, "_last_kick", 0.0)
    assert ready.kick(store=store, force=True) is True
    assert ready.kick(store=store, force=True) is False      # one at a time
    gate.set()
    for _ in range(100):
        if not ready._running.locked():
            break
        time.sleep(0.01)
    assert len(runs) == 1 and not ready._running.locked()
    store.set_meta("auto_prepare", False)
    assert ready.kick(store=store, force=True) is False      # no consent, no thread


# ---------------------------------------------------------------- day_hook: ready, weak, slides

def test_attach_adds_ready_weak_and_slides(store, monkeypatch):
    l7 = deck(store, "Lecture 7: Trees", [page(1, "Trees"), page(2, "Balanced trees")])
    l8 = deck(store, "Lecture 8: Heaps", [page(1, "Heaps")])
    w5 = deck(store, "Week 5: Hashing", [page(1, "Hashing", topics=["hashing"]), page(2, "Buckets")])
    store.upsert_assignments([
        task("pa3", "PA 3", at(17, d=20), details="Lectures 7 and 8 cover this."),
        task("quiz4", "Quiz 4", at(10, d=19), kind="quiz", details="Week 5"),
        task("hw", "HW 6 on trees", at(21), details="See lecture 7"),
    ])
    mat = store.save_material({"kind": "quiz", "title": "Hashing quiz", "course_id": "canvas:7",
                               "assignment_id": "quiz4", "body": QUIZ})
    store.set_meta(ready.PREPARED, {"quiz4": {"material_id": mat["id"], "at": at(8), "count": 10,
                                              "slides": [{"source_id": w5, "deck": "Week 5: Hashing",
                                                          "pages": [1, 2]}]}})
    old = store.save_material({"kind": "quiz", "title": "Old", "course_id": "canvas:7", "body": QUIZ})
    store.record_misses(old["id"], [{"question": i, "source_id": l7, "page": p, "topic": "pointers"}
                                    for i, p in ((0, 1), (1, 2), (2, 2))])
    monkeypatch.setattr(day_hook, "store_path", lambda: store.path)
    monkeypatch.setattr(day_hook, "study_store", lambda: store)
    kicked = []
    monkeypatch.setattr(ready, "kick", lambda *a, **k: kicked.append(k) or False)
    result = {"day": DAY, "timezone": TZ, "blocks": [], "deadlines": []}
    day_hook.attach(result, now=at(9), timezone=TZ)
    study = result["study"]
    assert [r["assignment_id"] for r in study["ready"]] == ["hw", "quiz4", "pa3"]
    by = {r["assignment_id"]: r for r in study["ready"]}
    assert by["pa3"]["text"] == "PA 3 is due Friday. Lectures 7 and 8 cover it."
    assert by["pa3"]["material_id"] is None and by["pa3"]["kind"] == "assignment"
    assert by["quiz4"]["text"] == "CSE 42 quiz Thursday. I made a 10-question quiz from week 5."
    assert by["quiz4"]["material_id"] == mat["id"]
    assert set(by["pa3"]) == {"id", "kind", "assignment_id", "course", "title", "due_ts", "text",
                              "material_id", "slides"}
    assert {s["source_id"] for s in by["pa3"]["slides"]} == {l7, l8}
    assert study["weak"] == [{"id": "weak:canvas:7:pointers", "course": "CSE 42", "topic": "pointers",
                              "count": 3, "text": "You missed 3 on pointers; here are those two slides again.",
                              "slides": [{"source_id": l7, "deck": "Lecture 7: Trees", "pages": [1, 2]}]}]
    hw = next(d for d in result["deadlines"] if d["id"] == "study:hw")
    assert hw["covers_text"] == "Lecture 7 covers it."
    assert hw["slides"] == [{"source_id": l7, "deck": "Lecture 7: Trees", "pages": [1, 2]}]
    assert hw["review"] == {"minutes": 15, "pages": [{"source_id": l7, "page": 1}, {"source_id": l7, "page": 2}]}
    block = next(b for b in result["blocks"] if b["id"] == "study-block:hw")
    assert block["covers_text"] == "Lecture 7 covers it." and "review" not in block
    assert kicked   # a background preparation was offered the store


def test_attach_without_pages_keeps_the_old_shape_plus_empty_lists(store, monkeypatch):
    store.upsert_assignments([task("hw", "HW 6", at(21))])
    monkeypatch.setattr(day_hook, "store_path", lambda: store.path)
    monkeypatch.setattr(day_hook, "study_store", lambda: store)
    result = {"day": DAY, "timezone": TZ, "blocks": [], "deadlines": []}
    day_hook.attach(result, now=at(9), timezone=TZ)
    assert result["study"]["ready"] == [] and result["study"]["weak"] == []
    hw = result["deadlines"][0]
    assert hw["slides"] == [] and hw["covers_text"] is None and "review" not in hw
