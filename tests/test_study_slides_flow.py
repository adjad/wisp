"""Slides flow on a real StudyStore: pages, search, misses -> the today `study` ready and weak items. Made-up course data."""
import asyncio, time
from service.study.store import StudyStore
from service.study import day_hook, ready, generate as G

def test_real_store_flow(tmp_path, monkeypatch):
    s = StudyStore(tmp_path / "study.db")
    now = time.time()
    s.upsert_courses([{"id": "c1", "name": "Intro Programming", "code": "CS 20"}])
    s.upsert_assignments([{"id": "a1", "course_id": "c1", "title": "Quiz 4: Recursion", "kind": "quiz",
                           "due_ts": now + 30 * 3600, "status": "upcoming", "details": "Covers recursion and the call stack"}])
    src = s.save_source({"kind": "drive", "url": "https://docs.google.com/presentation/d/x1234567890/edit",
                         "title": "Week 5 - Recursion", "text": "Recursion slides", "course_id": "c1"})
    s.save_pages(src["id"], [{"page": i, "title": t, "points": ["p"], "figure": None, "topics": ["recursion"]}
                             for i, t in [(1, "Recursion basics"), (2, "Call stack"), (3, "Base case")]])
    assert s.search_pages("recursion call stack")[0]["deck"] == "Week 5 - Recursion"
    m = s.save_material({"kind": "quiz", "title": "Recursion quiz", "course_id": "c1", "assignment_id": None,
                         "model": "x", "sample": False, "body": {"questions": [
                             {"prompt": "q", "choices": ["a","b","c","d"], "answer": 0, "why": "w", "slide": {"source_id": src["id"], "page": 2}}] * 3}})
    s.record_misses(m["id"], [{"question": i, "source_id": src["id"], "page": 2, "topic": "recursion"} for i in range(3)])
    monkeypatch.setattr(day_hook, "study_store", lambda: s)
    monkeypatch.setattr(day_hook, "store_path", lambda: tmp_path / "study.db", raising=False)
    monkeypatch.setattr(ready, "kick", lambda *a, **k: False)
    result = {"deadlines": [], "blocks": []}
    day_hook.attach(result, now=now, timezone="UTC")
    st = result.get("study") or {}
    assert st.get("ready"), st
    assert st.get("weak"), st
