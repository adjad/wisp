"""The Study routes (service/study/api.py): the state's filtering and sorting, the import
flow, making and keeping material, every error mapping, body validation and the native guard.

The connector, the generator and the page parser are faked; their own files test them."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.study import api
from service.study.store import StudyStore

DAY = 86400
JSON = {"content-type": "application/json"}
QUIZ = {"questions": [{"prompt": "Q", "choices": ["a", "b", "c", "d"], "answer": 1, "why": "w"}] * 3}


class Fakes:
    def __init__(self):
        self.ready = True
        self.made: list[dict] = []
        self.extracted: list[str] = []
        self.make_error: Exception | None = None
        self.parsed: dict[str, SimpleNamespace] = {}
        self.extract_result: SimpleNamespace | Exception | None = None
        self.choices: list[tuple] = []
        self.extract_extra: list[dict] = []


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    f = Fakes()
    store = StudyStore(tmp_path / "study" / "study.db")
    f.store = store
    monkeypatch.setattr(api, "study_store", lambda: store)
    monkeypatch.setattr(api, "study_exists", lambda: True)

    async def quiet():
        return None
    monkeypatch.setattr(api, "_changed", quiet)
    f.active_model = "google/gemini-3.8-flash"
    # Off by default, so every older test runs the ordinary (non-Gemini) limits and flow.
    f.long_context = False
    monkeypatch.setattr(api.connector, "status", lambda: {
        "provider": "openrouter", "model_id": "google/gemini-3.8-flash", "ready": f.ready,
        "source": "study" if f.ready else "none", "active_model_id": f.active_model if f.ready else None,
        "long_context": f.long_context and f.ready, "providers": []})

    def set_choice(provider, model_id):
        if provider not in ("openrouter", "gemini"):
            raise ValueError("Unknown Study provider")
        f.choices.append((provider, model_id))
        return {"provider": provider, "model_id": model_id, "ready": True, "source": "study", "providers": []}
    monkeypatch.setattr(api.connector, "set_choice", set_choice)

    async def make(kind, *, course, assignment, topic, count, context="", complete=None, **extra):
        f.made.append({"kind": kind, "course": course, "assignment": assignment, "topic": topic,
                       "count": count, "context": context, "extra": extra})
        if f.make_error:
            raise f.make_error
        return {"title": "Lists practice", "body": QUIZ}
    monkeypatch.setattr(api.generate, "make", make)

    def parse_page(url, title, text, *, now, timezone):
        return f.parsed.get(url) or SimpleNamespace(kind="other", courses=[], assignments=[])
    monkeypatch.setattr(api.canvas_parse, "parse_page", parse_page)
    monkeypatch.setattr(api.canvas_parse, "is_school_url", lambda url: "instructure" in url)

    async def extract(url, title, text, *, now, timezone, complete=None, **extra):
        f.extracted.append(url)
        f.extract_extra.append(extra)
        if isinstance(f.extract_result, Exception):
            raise f.extract_result
        return f.extract_result or SimpleNamespace(kind="other", courses=[], assignments=[])
    monkeypatch.setattr(api.generate, "extract_school_data", extract)
    return f


@pytest.fixture
def client(fakes):
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def course(cid="canvas:1", name="Introduction to Programming", **values):
    return {"id": cid, "name": name, "code": "CS 101", "grade": "B+", "score": 87.5,
            "url": None, "source": "canvas", "sample": False} | values


def item(aid, due, status="upcoming", course_id="canvas:1", **values):
    return {"id": aid, "course_id": course_id, "title": aid.title(), "kind": "assignment", "due_ts": due,
            "all_day": False, "points": 10.0, "score": None, "status": status, "details": "",
            "url": None, "source": "canvas", "sample": False} | values


def delete(client, url, **kw):
    return client.request("DELETE", url, headers=kw.pop("headers", JSON), **kw)


# --- the state ----------------------------------------------------------------------


def test_an_empty_state_has_every_key(client):
    got = client.get("/study/state").json()
    assert got == {"connector": got["connector"], "sample": False, "last_import": None,
                   "courses": [], "assignments": [], "materials": [], "sources": []}
    assert got["connector"]["model_id"] == "google/gemini-3.8-flash"


def test_the_state_filters_and_sorts_assignments(client, fakes):
    now = time.time()
    fakes.store.upsert_courses([course(), course("canvas:2", "Statistics")])
    fakes.store.upsert_assignments([
        item("later", now + 5 * DAY),
        item("soon", now + 3600, course_id="canvas:2"),
        item("undated", None),
        item("undated-done", None, status="graded"),
        item("missing-recent", now - 2 * DAY, status="missing"),
        item("late-recent", now - 13 * DAY, status="late"),
        item("missing-old", now - 15 * DAY, status="missing"),
        item("graded-past", now - DAY, status="graded"),
        item("submitted-past", now - 3600, status="submitted"),
        item("upcoming-past", now - 3600),
        item("submitted-future", now + 2 * DAY, status="submitted"),
        item("far", now + 9 * DAY),
    ])
    got = client.get("/study/state").json()
    ids = [a["id"] for a in got["assignments"]]
    assert ids == ["late-recent", "missing-recent", "soon", "submitted-future", "later", "far", "undated"]
    soon = got["assignments"][2]
    assert soon["course_name"] == "Statistics" and "details" not in soon and "source" not in soon
    assert set(soon) == {"id", "course_id", "course_name", "title", "kind", "due_ts", "all_day", "points",
                         "score", "status", "url", "sample"}
    counts = {c["id"]: c["due_count"] for c in got["courses"]}
    assert counts == {"canvas:1": 1, "canvas:2": 1}   # "later" and "soon"; submitted and far don't count
    assert set(got["courses"][0]) == {"id", "name", "code", "grade", "score", "url", "due_count", "active",
                                      "sample"}


def test_courses_list_due_then_graded_then_the_rest(client, fakes):
    now = time.time()
    fakes.store.upsert_courses([
        course("c-orient", "Orientation Module", grade=None, score=None, code=None),
        course("c-zoo", "Zoology", grade="B", score=None),
        course("c-art", "Art History", grade=None, score=88.0),
        course("c-bio", "Biology"),
        course("c-chem", "Chemistry", grade=None, score=None),
        course("c-alg", "Algebra", grade=None, score=None),
    ])
    fakes.store.upsert_assignments([item("bio-lab", now + DAY, course_id="c-bio"),
                                    item("chem-hw", now + 2 * DAY, course_id="c-chem")])
    got = client.get("/study/state").json()["courses"]
    assert [c["id"] for c in got] == ["c-bio", "c-chem", "c-art", "c-zoo", "c-alg", "c-orient"]
    assert {c["id"]: c["active"] for c in got} == {"c-bio": True, "c-chem": True, "c-art": True,
                                                   "c-zoo": True, "c-alg": False, "c-orient": False}


def test_an_assignment_for_an_unknown_course_gets_an_empty_course_name(client, fakes):
    fakes.store.upsert_assignments([item("orphan", time.time() + 3600, course_id="canvas:404")])
    assert client.get("/study/state").json()["assignments"][0]["course_name"] == ""


# --- sample and clearing ------------------------------------------------------------


def test_loading_and_clearing_the_sample(client, fakes):
    got = client.post("/study/sample", json={}).json()
    assert got["sample"] is True and len(got["courses"]) == 4 and len(got["materials"]) == 4
    assert all(a["sample"] for a in got["assignments"]) and got["assignments"]
    assert client.post("/study/sample", json={"timezone": "Europe/London"}).json()["courses"] == got["courses"]
    cleared = delete(client, "/study/data").json()
    assert cleared["sample"] is False and cleared["courses"] == [] and cleared["materials"] == []


def test_the_sample_refuses_extra_fields(client):
    assert client.post("/study/sample", json={"x": 1}).status_code == 422


# --- import -------------------------------------------------------------------------


def page(url, text="text"):
    return {"url": url, "title": "Canvas", "text": text}


def test_a_recognised_page_is_saved_and_clears_the_sample(client, fakes):
    client.post("/study/sample", json={})
    url = "https://school.instructure.com/grades"
    fakes.parsed[url] = SimpleNamespace(kind="grades", courses=[course()],
                                        assignments=[item("canvas:1:5", time.time() + DAY)])
    got = client.post("/study/import", json={"pages": [page(url)], "timezone": "America/New_York"}).json()
    assert got["courses"] == 1 and got["assignments"] == 1
    assert got["pages"] == [{"url": url, "kind": "grades", "recognized": True}]
    assert got["state"]["sample"] is False and [c["id"] for c in got["state"]["courses"]] == ["canvas:1"]
    assert got["state"]["last_import"] == pytest.approx(time.time(), abs=60)
    assert fakes.extracted == []


def test_an_unrecognised_school_page_goes_to_the_model_when_the_connector_is_ready(client, fakes):
    url = "https://school.instructure.com/courses/1/pages/syllabus"
    fakes.extract_result = SimpleNamespace(kind="assignments", courses=[course()],
                                           assignments=[item("canvas:1:9", time.time() + DAY)])
    got = client.post("/study/import", json={"pages": [page(url), page("https://news.example.com/")]}).json()
    assert fakes.extracted == [url]
    assert got["pages"] == [{"url": url, "kind": "assignments", "recognized": True},
                            {"url": "https://news.example.com/", "kind": "other", "recognized": False}]
    assert got["assignments"] == 1


def test_without_a_ready_connector_an_unrecognised_page_is_just_reported(client, fakes):
    fakes.ready = False
    client.post("/study/sample", json={})
    url = "https://school.instructure.com/courses/1/pages/syllabus"
    got = client.post("/study/import", json={"pages": [page(url)]}).json()
    assert fakes.extracted == [] and got["pages"] == [{"url": url, "kind": "other", "recognized": False}]
    assert got["courses"] == 0 and got["state"]["sample"] is True and got["state"]["last_import"] is None


@pytest.mark.parametrize("failure", [api.connector.StudyConnectorError("busy"), ValueError("bad json")])
def test_a_failed_extraction_leaves_that_page_unrecognised(client, fakes, failure):
    fakes.extract_result = failure
    url = "https://school.instructure.com/courses/1/pages/syllabus"
    got = client.post("/study/import", json={"pages": [page(url)]})
    assert got.status_code == 200 and got.json()["pages"][0]["recognized"] is False


@pytest.mark.parametrize("body", [
    {"pages": []},
    {"pages": [page(f"https://x.instructure.com/{i}") for i in range(13)]},
    {"pages": [page("https://x.instructure.com/", "x" * 150_001)]},
    {"pages": [{"url": "https://x.instructure.com/"}]},
    {"pages": [page("https://x.instructure.com/")], "extra": 1},
    {"pages": [page("https://x.instructure.com/")], "timezone": "Mars/Olympus"},
    {"pages": "nope"},
])
def test_bad_import_bodies_are_422_with_the_error_shape(client, body):
    got = client.post("/study/import", json=body)
    assert got.status_code == 422 and got.json()["detail"] == "invalid" and got.json()["message"]


def test_a_body_that_is_not_an_object_is_422(client):
    got = client.post("/study/import", content=b"[1, 2]", headers=JSON)
    assert got.status_code == 422 and set(got.json()) == {"detail", "message"}
    assert client.post("/study/import", content=b"{not json", headers=JSON).status_code == 422


# --- connector ----------------------------------------------------------------------


def test_choosing_a_connector_returns_its_state(client, fakes):
    got = client.post("/study/connector", json={"provider": "gemini", "model_id": "gemini-3.8-flash"})
    assert got.status_code == 200 and got.json()["provider"] == "gemini"
    assert fakes.choices == [("gemini", "gemini-3.8-flash")]
    bad = client.post("/study/connector", json={"provider": "elsewhere", "model_id": "x"})
    assert bad.status_code == 422 and bad.json()["detail"] == "invalid"


# --- generate and materials ---------------------------------------------------------


def seeded(fakes):
    now = time.time()
    fakes.store.upsert_courses([course()])
    fakes.store.upsert_assignments([item("canvas:1:5", now + DAY, title="Lab 5: Lists",
                                         details="Write three list functions."),
                                    item("canvas:1:6", now + 2 * DAY, title="Quiz 2")])


def test_making_material_from_an_assignment(client, fakes):
    seeded(fakes)
    got = client.post("/study/generate", json={"kind": "quiz", "assignment_id": "canvas:1:5", "count": 5})
    assert got.status_code == 200
    material = got.json()
    assert material["kind"] == "quiz" and material["title"] == "Lists practice" and material["body"] == QUIZ
    assert material["course_id"] == "canvas:1" and material["assignment_id"] == "canvas:1:5"
    assert material["model"] == "google/gemini-3.8-flash" and material["sample"] is False
    assert material["best_score"] is None and material["count"] == 3
    call = fakes.made[0]
    assert call["course"]["id"] == "canvas:1" and call["assignment"]["course_name"].startswith("Introduction")
    # The description travels once, on the assignment (generate.make puts it in the block),
    # never duplicated into the context.
    assert call["count"] == 5 and "Write three list functions." not in call["context"]
    assert call["assignment"]["details"] == "Write three list functions."
    assert "Quiz 2" in call["context"] and "Lab 5: Lists\n" not in call["context"]
    assert client.get("/study/state").json()["materials"][0]["id"] == material["id"]


def test_a_material_records_the_model_that_answered_not_the_study_choice(client, fakes):
    seeded(fakes)
    fakes.active_model = "google/gemini-3.9-pro"   # the Intel cloud fallback's own model
    got = client.post("/study/generate", json={"kind": "quiz", "assignment_id": "canvas:1:5"}).json()
    assert got["model"] == "google/gemini-3.9-pro"
    assert client.get("/study/state").json()["connector"]["model_id"] == "google/gemini-3.8-flash"


def test_import_converges_a_course_seen_on_the_dashboard_and_its_course_page(client, fakes):
    now = time.time()
    dash, page_url = "https://school.instructure.com/", "https://school.instructure.com/courses/12345/assignments"
    fakes.parsed[dash] = SimpleNamespace(kind="dashboard", courses=[
        course("canvas:hash1", "Introduction to Programming", grade=None, score=None,
               merge_key="introduction to programming")],
        assignments=[item("canvas:hashA", now + DAY, course_id="canvas:hash1", title="Lab 5: Lists",
                          points=None)])
    fakes.parsed[page_url] = SimpleNamespace(kind="assignments", courses=[
        course("canvas:12345", "Introduction to Programming", grade=None, score=None,
               url="https://school.instructure.com/courses/12345", merge_key="introduction to programming")],
        assignments=[item("canvas:12345:abc", now + DAY, course_id="canvas:12345", title="Lab 5: Lists",
                          points=20.0)])
    first = client.post("/study/import", json={"pages": [page(dash)]}).json()
    assert first["courses"] == 1 and first["assignments"] == 1
    got = client.post("/study/import", json={"pages": [page(page_url)]}).json()
    assert got["courses"] == 1 and got["assignments"] == 1
    state = got["state"]
    assert [c["id"] for c in state["courses"]] == ["canvas:12345"]
    assert state["courses"][0]["due_count"] == 1 and state["courses"][0]["active"] is True
    assert [(a["id"], a["course_id"], a["points"]) for a in state["assignments"]] == [
        ("canvas:hashA", "canvas:12345", 20.0)]
    both = client.post("/study/import", json={"pages": [page(dash), page(page_url)]}).json()
    assert both["state"]["courses"] == state["courses"] and both["state"]["assignments"] == state["assignments"]


def test_making_material_from_a_course_or_a_topic(client, fakes):
    seeded(fakes)
    assert client.post("/study/generate", json={"kind": "guide", "course_id": "canvas:1"}).status_code == 200
    assert "Lab 5: Lists" in fakes.made[0]["context"] and fakes.made[0]["assignment"] is None
    got = client.post("/study/generate", json={"kind": "flashcards", "topic": "  Photosynthesis "})
    assert got.status_code == 200 and fakes.made[1]["topic"] == "Photosynthesis"
    assert got.json()["course_id"] is None


@pytest.mark.parametrize("body,status", [
    ({"kind": "quiz", "assignment_id": "nope"}, 404),
    ({"kind": "quiz", "course_id": "nope"}, 404),
    ({"kind": "quiz"}, 422),
    ({"kind": "quiz", "topic": "   "}, 422),
    ({"kind": "essay", "topic": "x"}, 422),
    ({"kind": "quiz", "topic": "x", "count": 0}, 422),
    ({"kind": "quiz", "topic": "x", "count": "5"}, 422),
    ({"kind": "quiz", "topic": "x", "mode": "fast"}, 422),
])
def test_generate_refusals(client, fakes, body, status):
    seeded(fakes)
    got = client.post("/study/generate", json=body)
    assert got.status_code == status and set(got.json()) == {"detail", "message"}
    assert got.json()["detail"] == ("not_found" if status == 404 else "invalid")
    assert fakes.made == []


@pytest.mark.parametrize("code,status", [("missing", 409), ("rejected", 502), ("busy", 502),
                                         ("unreachable", 502), ("bad_reply", 502)])
def test_connector_failures_map_to_the_contract(client, fakes, code, status):
    fakes.make_error = api.connector.StudyConnectorError(code)
    got = client.post("/study/generate", json={"kind": "quiz", "topic": "Loops"})
    assert got.status_code == status
    assert got.json() == {"detail": code, "message": api.connector.StudyConnectorError(code).message}
    assert "sk-" not in got.text and client.get("/study/state").json()["materials"] == []


def test_a_reply_that_fails_validation_is_bad_reply(client, fakes):
    fakes.make_error = ValueError("quiz needs 3 questions")
    got = client.post("/study/generate", json={"kind": "quiz", "topic": "Loops"})
    assert got.status_code == 502 and got.json()["detail"] == "bad_reply"
    assert "3 questions" not in got.json()["message"]


def test_a_material_is_read_scored_and_deleted(client, fakes):
    mid = client.post("/study/generate", json={"kind": "quiz", "topic": "Loops"}).json()["id"]
    assert client.get(f"/study/materials/{mid}").json()["body"] == QUIZ
    assert client.post(f"/study/materials/{mid}/attempt", json={"correct": 2, "total": 3}).json() == \
        {"best_score": pytest.approx(2 / 3)}
    assert client.post(f"/study/materials/{mid}/attempt", json={"correct": 1, "total": 3}).json()["best_score"] \
        == pytest.approx(2 / 3)
    assert client.get("/study/state").json()["materials"][0]["best_score"] == pytest.approx(2 / 3)
    assert delete(client, f"/study/materials/{mid}").json() == {"deleted": True}
    for got in (client.get(f"/study/materials/{mid}"), delete(client, f"/study/materials/{mid}"),
                client.post(f"/study/materials/{mid}/attempt", json={"correct": 1, "total": 1})):
        assert got.status_code == 404 and got.json()["detail"] == "not_found" and got.json()["message"]


@pytest.mark.parametrize("body", [{"correct": 4, "total": 3}, {"correct": -1, "total": 3},
                                  {"correct": 1, "total": 0}, {"correct": 1.5, "total": 3}, {"correct": 1}])
def test_bad_attempts_are_422(client, fakes, body):
    mid = client.post("/study/generate", json={"kind": "quiz", "topic": "Loops"}).json()["id"]
    assert client.post(f"/study/materials/{mid}/attempt", json=body).status_code == 422


# --- the native guard ---------------------------------------------------------------


@pytest.mark.parametrize("method,url", [
    ("POST", "/study/import"), ("POST", "/study/connector"), ("POST", "/study/generate"),
    ("POST", "/study/sample"), ("POST", "/study/materials/m_1/attempt"),
    ("DELETE", "/study/materials/m_1"), ("DELETE", "/study/data"),
])
def test_every_change_is_native_only(client, fakes, method, url):
    fakes.store.upsert_courses([course()])
    from_page = client.request(method, url, json={}, headers={"origin": "https://evil.example"})
    assert from_page.status_code == 403
    as_form = client.request(method, url, content=b"a=1", headers={"content-type": "text/plain"})
    assert as_form.status_code == 415
    assert fakes.made == [] and len(fakes.store.courses()) == 1


# --- cost bounds --------------------------------------------------------------------


def _school_pages(n):
    return [page(f"https://school.instructure.com/courses/1/pages/p{i}") for i in range(n)]


def test_at_most_three_pages_per_import_go_to_the_model(client, fakes):
    got = client.post("/study/import", json={"pages": _school_pages(6)}).json()
    assert len(fakes.extracted) == api.MODEL_PAGES == 3
    assert [p["recognized"] for p in got["pages"]] == [False] * 6


@pytest.mark.parametrize("code", ["rejected", "busy", "missing", "unreachable"])
def test_a_refused_or_busy_model_reads_no_more_pages(client, fakes, code):
    fakes.extract_result = api.connector.StudyConnectorError(code)
    got = client.post("/study/import", json={"pages": _school_pages(3)})
    assert got.status_code == 200 and len(fakes.extracted) == 1
    assert [p["recognized"] for p in got.json()["pages"]] == [False] * 3


def test_a_bad_reply_on_one_page_still_lets_the_next_be_read(client, fakes):
    fakes.extract_result = api.connector.StudyConnectorError("bad_reply")
    client.post("/study/import", json={"pages": _school_pages(3)})
    assert len(fakes.extracted) == 3


def test_the_import_model_deadline_stops_reading(client, fakes, monkeypatch):
    import asyncio
    monkeypatch.setattr(api, "IMPORT_MODEL_S", 0.2)

    async def slow(url, title, text, *, now, timezone, complete=None):
        fakes.extracted.append(url)
        await asyncio.sleep(5)
    monkeypatch.setattr(api.generate, "extract_school_data", slow)
    started = time.monotonic()
    got = client.post("/study/import", json={"pages": _school_pages(3)})
    assert got.status_code == 200 and time.monotonic() - started < 2
    assert len(fakes.extracted) == 1 and not any(p["recognized"] for p in got.json()["pages"])


def test_one_import_sends_at_most_six_paid_posts(client, fakes, monkeypatch):
    import httpx
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
    transport = httpx.MockTransport(handler)

    async def extract(url, title, text, *, now, timezone, complete=None):
        fakes.extracted.append(url)
        await api.connector.complete_json("s", "u", key_reader=lambda p: "sk-or-test-key", transport=transport)
    monkeypatch.setattr(api.generate, "extract_school_data", extract)
    client.post("/study/import", json={"pages": _school_pages(12)})
    assert len(fakes.extracted) == 3 and len(sent) == api.IMPORT_POSTS == 6


def test_a_generate_past_its_deadline_is_unreachable(client, fakes, monkeypatch):
    import asyncio
    monkeypatch.setattr(api, "GENERATE_S", 0.1)

    async def slow(kind, **kw):
        await asyncio.sleep(5)
    monkeypatch.setattr(api.generate, "make", slow)
    started = time.monotonic()
    got = client.post("/study/generate", json={"kind": "quiz", "topic": "Loops"})
    assert time.monotonic() - started < 2
    assert got.status_code == 502 and got.json()["detail"] == "unreachable"


def test_the_import_body_cap_takes_a_full_import_and_refuses_more(fakes):
    import json as _json
    from service import local_http
    app = FastAPI()
    app.include_router(api.router)
    app.add_middleware(local_http.BodyLimit)
    big = TestClient(app)
    text = ("Lab 5 due Fri 11:59pm.\n" * 7000)[:api.PAGE_CHARS]
    full = {"pages": [{"url": f"https://example.org/{i}", "title": "Canvas", "text": text}
                      for i in range(api.MAX_PAGES)]}
    raw = _json.dumps(full).encode()
    assert len(raw) < local_http.STUDY_IMPORT_BODY_LIMIT
    assert big.post("/study/import", content=raw, headers=JSON).status_code == 200
    over = b'{"pages": [' + b" " * local_http.STUDY_IMPORT_BODY_LIMIT + b"]}"
    refused = big.post("/study/import", content=over, headers=JSON)
    assert refused.status_code == 413 and "page text" in refused.json()["detail"]
    assert local_http._LIMITED_PATHS["/study/import"] == local_http.STUDY_IMPORT_BODY_LIMIT


# --- long context (a Gemini Study model) ----------------------------------------------


def test_without_long_context_the_import_is_unchanged_and_saves_no_page_sources(client, fakes):
    fakes.extract_result = SimpleNamespace(kind="assignments", courses=[course()], assignments=[])
    got = client.post("/study/import", json={"pages": _school_pages(6)}).json()
    assert len(fakes.extracted) == 3 and fakes.extract_extra == [{}] * 3
    assert [p["recognized"] for p in got["pages"]] == [True] * 3 + [False] * 3
    assert fakes.store.sources() == [] and got["state"]["sources"] == []


def test_long_context_reads_twelve_pages_four_at_a_time_in_request_order(client, fakes, monkeypatch):
    import asyncio
    fakes.long_context = True
    active = {"now": 0, "most": 0}

    async def extract(url, title, text, *, now, timezone, complete=None, **extra):
        fakes.extracted.append(url)
        fakes.extract_extra.append(extra)
        n = int(url.rsplit("p", 1)[1])
        active["now"] += 1
        active["most"] = max(active["most"], active["now"])
        await asyncio.sleep(0.02 * (12 - n) / 12)   # later pages finish first
        active["now"] -= 1
        if n == 5:
            return SimpleNamespace(kind="other", courses=[], assignments=[])
        return SimpleNamespace(kind="course_grades", courses=[course(f"canvas:{n}", f"Course {n}")],
                               assignments=[])
    monkeypatch.setattr(api.generate, "extract_school_data", extract)
    pages = _school_pages(12)
    got = client.post("/study/import", json={"pages": pages}).json()
    assert len(fakes.extracted) == api.LONG_MODEL_PAGES == 12
    assert active["most"] == api.LONG_CONCURRENCY == 4
    assert all(e == {"long_context": True} for e in fakes.extract_extra)
    assert [p["url"] for p in got["pages"]] == [p["url"] for p in pages]
    assert [p["kind"] for p in got["pages"]] == ["course_grades"] * 5 + ["other"] + ["course_grades"] * 6
    assert got["courses"] == 11
    # Every page the model read is a "page" source, tied to its one course; page 5 named none.
    sources = {s["url"]: s for s in fakes.store.sources()}
    assert len(sources) == 12 and all(s["kind"] == "page" for s in sources.values())
    assert sources[pages[0]["url"]]["course_id"] == "canvas:0"
    assert sources[pages[5]["url"]]["course_id"] is None
    assert fakes.store.source(sources[pages[3]["url"]]["id"])["text"] == "text"
    assert len(got["state"]["sources"]) == 12
    # Importing again replaces each page source rather than adding another.
    client.post("/study/import", json={"pages": pages})
    assert {s["id"] for s in fakes.store.sources()} == {s["id"] for s in sources.values()}


def test_long_context_keeps_recognised_school_pages_and_skips_other_sites(client, fakes):
    fakes.long_context = True
    school, other = "https://school.instructure.com/courses/1/grades", "https://example.org/notes"
    fakes.parsed[school] = SimpleNamespace(kind="course_grades", courses=[course()], assignments=[])
    fakes.parsed[other] = SimpleNamespace(kind="course_grades", courses=[course("canvas:2", "Art")],
                                          assignments=[])
    client.post("/study/import", json={"pages": [page(school), page(other)]})
    assert fakes.extracted == []
    assert [(s["url"], s["course_id"]) for s in fakes.store.sources()] == [(school, "canvas:1")]


def test_long_context_import_sends_at_most_twenty_four_paid_posts(client, fakes, monkeypatch):
    import httpx
    fakes.long_context = True
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
    transport = httpx.MockTransport(handler)

    async def extract(url, title, text, *, now, timezone, complete=None, **extra):
        fakes.extracted.append(url)
        await api.connector.complete_json("s", "u", key_reader=lambda p: "sk-or-test-key", transport=transport)
    monkeypatch.setattr(api.generate, "extract_school_data", extract)
    got = client.post("/study/import", json={"pages": _school_pages(12)})
    assert got.status_code == 200
    assert len(sent) == api.LONG_IMPORT_POSTS == 24
    assert fakes.store.sources() == []   # no page was read, none recognised


@pytest.mark.parametrize("code", ["rejected", "busy", "missing", "unreachable"])
def test_long_context_stops_starting_pages_after_a_refusal(client, fakes, monkeypatch, code):
    import asyncio
    fakes.long_context = True

    async def refused(url, title, text, *, now, timezone, complete=None, **extra):
        fakes.extracted.append(url)
        await asyncio.sleep(0.01)
        raise api.connector.StudyConnectorError(code)
    monkeypatch.setattr(api.generate, "extract_school_data", refused)
    got = client.post("/study/import", json={"pages": _school_pages(12)})
    assert got.status_code == 200
    assert len(fakes.extracted) == api.LONG_CONCURRENCY   # the first four were already running


def test_long_context_import_deadline(client, fakes, monkeypatch):
    import asyncio
    fakes.long_context = True
    monkeypatch.setattr(api, "LONG_IMPORT_MODEL_S", 0.2)

    async def slow(url, title, text, *, now, timezone, complete=None, **extra):
        fakes.extracted.append(url)
        await asyncio.sleep(5)
    monkeypatch.setattr(api.generate, "extract_school_data", slow)
    started = time.monotonic()
    got = client.post("/study/import", json={"pages": _school_pages(12)})
    assert got.status_code == 200 and time.monotonic() - started < 2
    assert len(fakes.extracted) == 4 and not any(p["recognized"] for p in got.json()["pages"])


def _sources(fakes):
    a = fakes.store.save_source({"kind": "drive", "url": "https://docs.google.com/document/d/aaa",
                                 "title": "Notes A", "text": "alpha", "course_id": "canvas:1"})
    b = fakes.store.save_source({"kind": "drive", "url": "https://docs.google.com/document/d/bbb",
                                 "title": "Notes B", "text": "beta", "course_id": None})
    c = fakes.store.save_source({"kind": "page", "url": "https://school.instructure.com/courses/1/pages/x",
                                 "title": "Week 3", "text": "gamma", "course_id": "canvas:1"})
    return a, b, c


def test_generate_from_chosen_sources(client, fakes):
    seeded(fakes)
    a, b, _ = _sources(fakes)
    got = client.post("/study/generate", json={"kind": "guide", "source_ids": [b["id"], a["id"], b["id"]]})
    assert got.status_code == 200 and got.json()["course_id"] is None
    extra = fakes.made[0]["extra"]
    assert [s["text"] for s in extra["sources"]] == ["beta", "alpha"] and extra["long_context"] is False
    fakes.long_context = True
    client.post("/study/generate", json={"kind": "quiz", "course_id": "canvas:1", "source_ids": [a["id"]]})
    assert fakes.made[1]["extra"]["long_context"] is True
    assert fakes.made[1]["course"]["id"] == "canvas:1"


def test_generate_without_sources_passes_nothing_new(client, fakes):
    seeded(fakes)
    _sources(fakes)
    fakes.long_context = True
    client.post("/study/generate", json={"kind": "quiz", "course_id": "canvas:1"})
    client.post("/study/generate", json={"kind": "quiz", "course_id": "canvas:1", "source_ids": []})
    assert [m["extra"] for m in fakes.made] == [{}, {}]


def test_whole_course_uses_every_course_source_and_assignment_description(client, fakes):
    seeded(fakes)
    a, b, c = _sources(fakes)
    fakes.long_context = True
    got = client.post("/study/generate", json={"kind": "guide", "course_id": "canvas:1", "whole_course": True,
                                               "source_ids": [b["id"]]})
    assert got.status_code == 200 and got.json()["course_id"] == "canvas:1"
    call = fakes.made[0]
    assert [s["id"] for s in call["extra"]["sources"]] == [b["id"], c["id"], a["id"]]
    assert call["extra"]["long_context"] is True
    assert "Write three list functions." in call["context"] and "Quiz 2" in call["context"]


def test_whole_course_without_long_context_keeps_the_ordinary_context_cap(client, fakes):
    seeded(fakes)
    fakes.store.upsert_assignments([item("canvas:1:9", time.time() + DAY, title="Essay", details="x" * 4000),
                                    item("canvas:1:10", time.time() + DAY, title="Essay 2", details="y" * 4000)])
    client.post("/study/generate", json={"kind": "guide", "course_id": "canvas:1", "whole_course": True})
    call = fakes.made[0]
    assert call["extra"] == {"sources": [], "long_context": False}
    assert len(call["context"]) <= api.CONTEXT_MAX


@pytest.mark.parametrize("body,status", [
    ({"kind": "guide", "whole_course": True}, 422),
    ({"kind": "guide", "whole_course": "yes", "course_id": "canvas:1"}, 422),
    ({"kind": "guide", "source_ids": ["src_unknown"]}, 404),
    ({"kind": "guide", "source_ids": [f"src_{i}" for i in range(21)]}, 422),
    ({"kind": "guide", "source_ids": ["x" * 101]}, 422),
    ({"kind": "guide", "source_ids": "src_1"}, 422),
    ({"kind": "guide", "whole_course": True, "course_id": "canvas:9"}, 404),
])
def test_generate_source_refusals(client, fakes, body, status):
    seeded(fakes)
    got = client.post("/study/generate", json=body)
    assert got.status_code == status and fakes.made == []
    assert got.json()["detail"] == ("not_found" if status == 404 else "invalid")


def test_deleting_a_source_returns_the_state(client, fakes):
    a, b, _ = _sources(fakes)
    assert [s["id"] for s in client.get("/study/state").json()["sources"]][-1] == a["id"]
    got = delete(client, f"/study/sources/{a['id']}")
    assert got.status_code == 200 and a["id"] not in {s["id"] for s in got.json()["state"]["sources"]}
    assert delete(client, f"/study/sources/{a['id']}").status_code == 404
    with_origin = client.request("DELETE", f"/study/sources/{b['id']}", headers=JSON | {"origin": "https://x"})
    assert with_origin.status_code == 403 and fakes.store.source(b["id"]) is not None


def test_the_drive_routes_can_reuse_the_study_helpers():
    assert callable(api._answer) and callable(api._state) and issubclass(api.Refusal, Exception)


# --- zero footprint -------------------------------------------------------------------


def test_reading_study_on_a_fresh_mac_creates_nothing(tmp_path, monkeypatch):
    import subprocess
    from service.study import connector, day_hook
    home = tmp_path / "fresh-home"
    home.mkdir()
    monkeypatch.setenv("WISP_HOME", str(home))
    monkeypatch.setattr(connector, "_security",
                        lambda args: subprocess.CompletedProcess(args, 44, b"", b"not found"))
    monkeypatch.setattr(connector, "_cloud_target", lambda: None)
    monkeypatch.setattr(connector, "_cloud_cache", None)
    connector.forget_key()
    app = FastAPI()
    app.include_router(api.router)
    reader = TestClient(app)
    got = reader.get("/study/state")
    assert got.status_code == 200
    assert got.json() | {"connector": None} == {"connector": None, "sample": False, "last_import": None,
                                                "courses": [], "assignments": [], "materials": [],
                                                "sources": []}
    assert got.json()["connector"]["long_context"] is False
    assert reader.get("/study/materials/m_nope").status_code == 404
    assert delete(reader, "/study/materials/m_nope").status_code == 404
    assert reader.post("/study/materials/m_nope/attempt", json={"correct": 1, "total": 2}).status_code == 404
    assert delete(reader, "/study/data").status_code == 200
    assert delete(reader, "/study/sources/src_nope").status_code == 404
    assert reader.post("/study/generate", json={"kind": "quiz", "source_ids": ["src_nope"]}).status_code == 404
    assert reader.post("/study/generate", json={"kind": "quiz", "course_id": "canvas:9"}).status_code == 404
    result = {"day": "2026-11-14", "deadlines": [], "blocks": []}
    day_hook.attach(result, now=time.time(), timezone="UTC")
    assert "study" not in result
    connector.status()
    assert not (home / "study").exists() and list(home.iterdir()) == []
    # The first real save is what creates it.
    connector.set_choice("gemini", "")
    assert (home / "study" / "settings.json").exists() and not (home / "study" / "study.db").exists()


def test_the_router_is_mounted_in_the_app():
    from pathlib import Path
    main = (Path(api.__file__).resolve().parents[1] / "main.py").read_text()
    assert "from service.study.api import router as study_router" in main
    assert "app.include_router(study_router)" in main


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_study_api.py"' in manifest
