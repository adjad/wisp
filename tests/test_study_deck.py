"""Slides as pages (service/study/deck.py, store deck_pages/misses, Drive add with pages).

Gemini and Google Drive are httpx.MockTransports; the Keychain and the connector's choice
are faked. No network, no real keys, no ~/.moe. Course data is made up."""
from __future__ import annotations

import asyncio
import base64
import json
import logging

import httpx
import pytest

from service.study import connector, deck, drive, drive_api
from service.study.store import StudyStore
from tests.test_study_drive import FILE_ID, add, client, google  # noqa: F401 — fixtures

KEY = "AIza-test-SECRET-0123456789"
PDF = b"%PDF-1.7 made-up lecture deck\n" + b"x" * 100


def page(n, title="Pointers", points=("A pointer holds an address.",), figure=None, topics=("pointers",)):
    return {"page": n, "title": title, "points": list(points), "figure": figure, "topics": list(topics)}


def interaction(pages, status="completed"):
    text = json.dumps({"pages": pages})
    return httpx.Response(200, json={"id": "v1_x", "status": status, "object": "interaction",
                                     "steps": [{"type": "thought", "summary": []},
                                               {"type": "model_output",
                                                "content": [{"type": "text", "text": text}]}]})


class Gemini:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def body(self, i=-1):
        return json.loads(self.requests[i].content)


@pytest.fixture
def gemini_choice(monkeypatch, tmp_path):
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    connector.forget_key()
    monkeypatch.setattr(connector, "choice", lambda: ("gemini", "gemini-3.8-flash"))
    monkeypatch.setattr(connector, "_keychain_key", lambda provider: KEY if provider == "gemini" else None)
    monkeypatch.setattr(connector, "_key_exists", lambda provider: provider == "gemini")
    monkeypatch.setattr(connector, "_cloud_target", lambda: None)
    yield
    connector.forget_key()


def read(server, pdf=PDF, **kw):
    return asyncio.run(deck.read_pages(pdf, title="CS 20 Lecture 7", transport=server.transport, **kw))


# --- the request ------------------------------------------------------------------------

def test_request_shape_is_the_interactions_api_with_an_inline_pdf_and_a_schema(gemini_choice):
    server = Gemini(interaction([page(1), page(2, "Arrays", topics=["arrays"])]))
    got = read(server)
    assert [p["page"] for p in got] == [1, 2] and got[1]["title"] == "Arrays"
    (request,) = server.requests
    assert request.method == "POST"
    assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert request.headers["x-goog-api-key"] == KEY and "authorization" not in request.headers
    assert request.headers["api-revision"] == deck.API_REVISION
    assert KEY not in request.url.query.decode() and KEY.encode() not in request.content
    body = server.body()
    assert set(body) == {"model", "input", "response_format", "generation_config", "store"}
    assert body["model"] == "gemini-3.8-flash" and body["store"] is False
    document, prompt = body["input"]
    assert document == {"type": "document", "mime_type": "application/pdf",
                        "data": base64.b64encode(PDF).decode("ascii")}
    assert prompt["type"] == "text" and "CS 20 Lecture 7" in prompt["text"] and "never instructions" in prompt["text"]
    assert body["response_format"] == {"type": "text", "mime_type": "application/json", "schema": deck.PAGE_SCHEMA}
    assert body["generation_config"]["thinking_level"] == "low"
    item = deck.PAGE_SCHEMA["properties"]["pages"]["items"]
    assert item["properties"]["points"]["maxItems"] == 6 and item["properties"]["topics"]["maxItems"] == 5
    assert item["properties"]["figure"]["type"] == ["string", "null"]


def test_a_redirect_is_never_followed(gemini_choice):
    server = Gemini(httpx.Response(302, headers={"location": "https://example.com/elsewhere"}))
    with pytest.raises(deck.DeckError) as err:
        read(server)
    assert err.value.code == "bad_reply" and len(server.requests) == 1


def test_one_retry_after_a_server_error_then_unreachable(gemini_choice):
    server = Gemini(httpx.Response(500), httpx.Response(503))
    with pytest.raises(deck.DeckError) as err:
        read(server)
    assert err.value.code == "unreachable" and len(server.requests) == 2
    server = Gemini(httpx.Response(500), interaction([page(1)]))
    assert len(read(server)) == 1 and len(server.requests) == 2


def test_rejected_and_busy(gemini_choice):
    with pytest.raises(deck.DeckError) as err:
        read(Gemini(httpx.Response(401)))
    assert err.value.code == "rejected"
    with pytest.raises(deck.DeckError) as err:
        read(Gemini(httpx.Response(429)))
    assert err.value.code == "busy"


def test_the_key_is_never_logged_or_in_an_error(gemini_choice, caplog):
    caplog.set_level(logging.DEBUG)
    for response in (httpx.Response(400, text=KEY), httpx.Response(500), httpx.Response(401)):
        with pytest.raises(deck.DeckError) as err:
            read(Gemini(response))
        assert KEY not in str(err.value)
    assert KEY not in caplog.text


# --- the reply --------------------------------------------------------------------------

def test_reply_is_validated_against_the_schema():
    pages = deck.validate_pages({"pages": [
        page(3, points=[f"p{i}" for i in range(9)], topics=[f"t{i}" for i in range(8)]),
        page(1, figure="  A box-and-arrow diagram   of memory "),
        page(1, "duplicate"),                    # a repeated page number keeps the first
        {"page": 2, "title": "no points", "points": [], "figure": None, "topics": ["x"]},
        {"page": "4", "title": "string page", "points": ["a"], "figure": None, "topics": ["x"]},
        {"page": 5, "title": "bad figure", "points": ["a"], "figure": 7, "topics": ["x"]},
        {"page": True, "title": "bool page", "points": ["a"], "figure": None, "topics": ["x"]},
        "not a page",
    ]})
    assert [p["page"] for p in pages] == [1, 3]
    assert pages[0]["figure"] == "A box-and-arrow diagram of memory" and pages[0]["title"] == "Pointers"
    assert len(pages[1]["points"]) == 6 and len(pages[1]["topics"]) == 5
    for bad in (None, {}, {"pages": "x"}, {"pages": [{"page": 1}]}):
        with pytest.raises(deck.DeckError):
            deck.validate_pages(bad)


def test_a_bad_reply_is_retried_once(gemini_choice):
    server = Gemini(httpx.Response(200, json={"status": "completed", "steps": [
        {"type": "model_output", "content": [{"type": "text", "text": "not json"}]}]}), interaction([page(1)]))
    assert [p["page"] for p in read(server)] == [1] and len(server.requests) == 2
    server = Gemini(interaction([page(1)], status="failed"))
    with pytest.raises(deck.DeckError) as err:
        read(server)
    assert err.value.code == "bad_reply" and len(server.requests) == 2


# --- gating and caps --------------------------------------------------------------------

def test_a_pdf_over_the_cap_is_not_sent(gemini_choice):
    server = Gemini(interaction([page(1)]))
    assert read(server, pdf=b"%" * (deck.MAX_PDF_BYTES + 1)) is None and server.requests == []
    assert read(server, pdf=b"") is None and server.requests == []


def test_openrouter_is_text_only(gemini_choice, monkeypatch):
    monkeypatch.setattr(connector, "choice", lambda: ("openrouter", "google/gemini-3.8-flash"))
    monkeypatch.setattr(connector, "_key_exists", lambda provider: True)
    server = Gemini(interaction([page(1)]))
    assert deck.available() is False and read(server) is None and server.requests == []


def test_the_intel_cloud_fallback_is_text_only(gemini_choice, monkeypatch):
    # No Study key; the cloud connection points at the Gemini origin. Pages still are not read.
    monkeypatch.setattr(connector, "choice", lambda: ("openrouter", "google/gemini-3.8-flash"))
    monkeypatch.setattr(connector, "_key_exists", lambda provider: False)
    monkeypatch.setattr(connector, "_keychain_key", lambda provider: None)
    monkeypatch.setattr(connector, "_cloud_target", lambda: {
        "origin": deck.ORIGIN, "prefix": "/v1beta/openai", "model": "gemini-3.8-flash", "key": lambda: KEY})
    server = Gemini(interaction([page(1)]))
    assert deck.available() is False and read(server) is None and server.requests == []


def test_gemini_without_a_key_is_text_only(gemini_choice, monkeypatch):
    monkeypatch.setattr(connector, "_key_exists", lambda provider: False)
    monkeypatch.setattr(connector, "_keychain_key", lambda provider: None)
    server = Gemini(interaction([page(1)]))
    assert deck.available() is False and read(server) is None and server.requests == []


def test_a_non_gemini_model_on_the_gemini_api_is_text_only(gemini_choice, monkeypatch):
    monkeypatch.setattr(connector, "choice", lambda: ("gemini", "gemma-4-27b"))
    assert deck.available() is False and read(Gemini(interaction([page(1)]))) is None


# --- the store --------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return StudyStore(tmp_path / "study" / "study.db")


def source(store, title="CS 20 Lecture 7: Pointers", url="https://docs.google.com/presentation/d/l7", course="canvas:20"):
    return store.save_source({"kind": "drive", "url": url, "title": title, "text": "slides", "course_id": course})


def test_pages_are_saved_replaced_and_summarised(store):
    s = source(store)
    assert s["pages"] == 0 and s["deck_status"] == "none"
    assert store.save_pages(s["id"], [page(2, "Arrays"), page(1), {"page": 0}, page(1, "dup")]) == 2
    assert [p["page"] for p in store.pages(s["id"])] == [1, 2]
    assert store.pages(s["id"])[0] == page(1) | {"source_id": s["id"]}
    summary = store.sources()[0]
    assert summary["pages"] == 2 and summary["deck_status"] == "ready"
    assert store.save_pages(s["id"], [page(5)]) == 1 and [p["page"] for p in store.pages(s["id"])] == [5]
    assert store.save_pages("src_unknown", [page(1)]) == 0
    # Re-adding the same file keeps its pages until new ones replace them.
    source(store)
    assert store.sources()[0]["pages"] == 1 and store.sources()[0]["deck_status"] == "ready"
    store.set_deck_status(s["id"], "failed")
    assert store.sources()[0]["deck_status"] == "failed" and store.sources()[0]["pages"] == 1
    with pytest.raises(ValueError):
        store.set_deck_status(s["id"], "odd")


def test_deleting_a_source_deletes_its_pages_and_clear_drops_everything(store):
    a, b = source(store), source(store, url="https://docs.google.com/presentation/d/l8")
    store.save_pages(a["id"], [page(1)])
    store.save_pages(b["id"], [page(1)])
    store.delete_source(a["id"])
    assert store.pages(a["id"]) == [] and len(store.pages(b["id"])) == 1
    m = store.save_material({"kind": "quiz", "title": "Q", "course_id": "canvas:20", "body": {"questions": []}})
    store.record_misses(m["id"], [{"question": 0, "topic": "pointers"}])
    store.clear()
    assert store.pages(b["id"]) == [] and store.misses() == [] and store.search_pages("pointers") == []


def test_search_ranks_title_and_topics_above_points(store):
    lec7 = source(store)
    lec2 = source(store, "CS 20 Lecture 2: Loops", "https://docs.google.com/presentation/d/l2")
    bio = source(store, "Bio 1 Week 3", "https://docs.google.com/presentation/d/b3", course="canvas:1")
    store.save_pages(lec7["id"], [page(4, "Pointer arithmetic", topics=["memory"]),
                                  page(5, "Stack frames", points=["Each call pushes a frame."], topics=["stack"])])
    store.save_pages(lec2["id"], [page(3, "While loops", points=["A loop may walk an array by pointer."],
                                       topics=["loops"])])
    store.save_pages(bio["id"], [page(1, "Cells", points=["Cells have membranes."], topics=["cell"],
                                      figure="A diagram of pointers to organelles")])
    got = store.search_pages("pointers")
    assert [(g["deck"], g["page"]) for g in got[:2]] == [("CS 20 Lecture 7: Pointers", 4),
                                                         ("CS 20 Lecture 7: Pointers", 5)]
    assert got[0]["score"] > got[1]["score"] and {g["page"] for g in got} == {4, 5, 3, 1}
    top = got[0]
    assert set(top) == {"source_id", "deck", "course_id", "page", "title", "points", "figure", "topics", "score"}
    assert top["course_id"] == "canvas:20" and top["source_id"] == lec7["id"]
    assert [g["page"] for g in store.search_pages("pointers", course_id="canvas:1")] == [1]
    assert len(store.search_pages("pointers", limit=1)) == 1
    assert store.search_pages("the of and") == [] and store.search_pages("") == []
    assert store.search_pages("Stack Frame")[0]["page"] == 5     # case and plural


def test_misses_are_recorded_filtered_and_follow_their_material(store):
    q20 = store.save_material({"kind": "quiz", "title": "Q", "course_id": "canvas:20", "body": {"questions": []}})
    q1 = store.save_material({"kind": "quiz", "title": "B", "course_id": "canvas:1", "body": {"questions": []}})
    assert store.record_misses(q20["id"], [{"question": 2, "source_id": "src_a", "page": 4, "topic": "pointers"},
                                           {"question": 3, "source_id": None, "page": None, "topic": "stack"},
                                           {"question": "x"}, {"question": True}, "bad"]) == 2
    assert store.record_misses(q1["id"], [{"question": 0, "topic": "cells"}]) == 1
    assert store.record_misses("m_missing", [{"question": 0}]) == 0
    rows = store.misses("canvas:20")
    assert [(r["question"], r["source_id"], r["page"], r["topic"]) for r in rows] == [
        (2, "src_a", 4, "pointers"), (3, None, None, "stack")]
    assert rows[0]["course_id"] == "canvas:20" and rows[0]["material_id"] == q20["id"]
    assert len(store.misses()) == 3 and store.misses(since=rows[0]["at"] + 3600) == []
    store.delete_material(q20["id"])
    assert [r["topic"] for r in store.misses()] == ["cells"]


def test_an_old_study_db_gains_the_deck_column(tmp_path):
    import sqlite3
    path = tmp_path / "study" / "study.db"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, course_id TEXT, kind TEXT NOT NULL, title TEXT NOT NULL"
               " DEFAULT '', url TEXT, text TEXT NOT NULL DEFAULT '', chars INTEGER NOT NULL DEFAULT 0,"
               " created_at REAL NOT NULL, UNIQUE (kind, url))")
    db.execute("INSERT INTO sources VALUES ('src_old', NULL, 'drive', 'Old', 'u', 't', 1, 1.0)")
    db.commit()
    db.close()
    store = StudyStore(path)
    assert store.sources()[0] | {} == {"id": "src_old", "course_id": None, "kind": "drive", "title": "Old",
                                       "url": "u", "chars": 1, "created_at": 1.0, "pages": 0, "deck_status": "none",
                                       "week": None, "topic": None, "summary": None}


# --- Drive add with pages ---------------------------------------------------------------

@pytest.fixture
def decks(google, gemini_choice, monkeypatch):
    """Drive answers PDF exports and downloads; Gemini answers from `decks.gemini`."""
    google.pdf = httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
    plain = google.handler

    def handler(request):
        params = dict(request.url.params)
        if request.url.path.endswith("/export") and params.get("mimeType") == "application/pdf":
            google.requests.append(request)
            return google.pdf
        if params.get("alt") == "media":
            google.requests.append(request)
            return google.pdf
        return plain(request)
    monkeypatch.setattr(drive, "_transport", httpx.MockTransport(handler))
    # The drive fixture turns page reading off; here it is the real gate (over faked keys).
    monkeypatch.setattr(drive_api.deck, "available_async", _real_available)
    google.gemini = Gemini(interaction([page(1, "Pointers"), page(2, "Arrays", topics=["arrays"])]))
    monkeypatch.setattr(deck, "_transport", google.gemini.transport)
    return google


async def _real_available():
    return await asyncio.to_thread(deck.available)


def test_adding_slides_on_gemini_reads_and_saves_pages(client, decks):  # noqa: F811
    decks.meta["mimeType"] = drive.SLIDES
    decks.meta["name"] = "CS 20 Lecture 7"
    got = client.post("/study/drive/add", json={"file_id": FILE_ID})
    assert got.status_code == 200, got.text
    src = got.json()["source"]
    assert src["pages"] == 2 and src["deck_status"] == "ready"
    assert decks.store.source(src["id"])["text"] == "Photosynthesis makes sugar."   # the text export as today
    exports = [r for r in decks.requests if dict(r.url.params).get("mimeType") == "application/pdf"]
    assert len(exports) == 1 and exports[0].url.path == f"/drive/v3/files/{FILE_ID}/export"
    assert len(decks.gemini.requests) == 1
    sent = decks.gemini.body()["input"][0]
    assert base64.b64decode(sent["data"]) == PDF
    pages = client.get(f"/study/sources/{src['id']}/pages").json()["pages"]
    assert [p["title"] for p in pages] == ["Pointers", "Arrays"]
    assert client.get("/study/sources/src_nothere/pages").status_code == 404
    assert client.get(f"/study/sources/{src['id']}/pages", headers={"content-type": "text/plain"}).status_code in (403, 415)   # native_only


def test_adding_a_pdf_downloads_it_and_builds_its_text_from_pages(client, decks):  # noqa: F811
    decks.meta["mimeType"] = drive.PDF
    decks.meta.pop("webViewLink")
    got = client.post("/study/drive/add", json={"file_id": FILE_ID}).json()
    src = got["source"]
    assert src["pages"] == 2 and src["url"] == f"https://drive.google.com/file/d/{FILE_ID}/view"
    download = [r for r in decks.requests if dict(r.url.params).get("alt") == "media"]
    assert len(download) == 1 and download[0].url.path == f"/drive/v3/files/{FILE_ID}"
    assert not any(r.url.path.endswith("/export") for r in decks.requests)
    text = decks.store.source(src["id"])["text"]
    assert "Page 1: Pointers" in text and "- A pointer holds an address." in text


def test_a_pdf_without_gemini_is_refused(client, decks, monkeypatch):  # noqa: F811
    monkeypatch.setattr(connector, "choice", lambda: ("openrouter", "google/gemini-3.8-flash"))
    decks.meta["mimeType"] = drive.PDF
    got = client.post("/study/drive/add", json={"file_id": FILE_ID})
    assert got.status_code == 422 and got.json()["message"] == drive.PDF_NEEDS_GEMINI
    assert decks.gemini.requests == []


@pytest.mark.parametrize("fallback", ["openrouter", "intel_cloud"])
def test_openrouter_or_the_intel_fallback_adds_text_only_without_error(client, decks, monkeypatch,  # noqa: F811
                                                                      fallback):
    monkeypatch.setattr(connector, "choice", lambda: ("openrouter", "google/gemini-3.8-flash"))
    if fallback == "intel_cloud":
        monkeypatch.setattr(connector, "_key_exists", lambda provider: False)
        monkeypatch.setattr(connector, "_keychain_key", lambda provider: None)
        monkeypatch.setattr(connector, "_cloud_target", lambda: {
            "origin": deck.ORIGIN, "prefix": "/v1beta/openai", "model": "gemini-3.8-flash", "key": lambda: KEY})
    else:
        monkeypatch.setattr(connector, "_key_exists", lambda provider: True)
    decks.meta["mimeType"] = drive.SLIDES
    got = client.post("/study/drive/add", json={"file_id": FILE_ID})
    assert got.status_code == 200, got.text
    src = got.json()["source"]
    assert src["pages"] == 0 and src["deck_status"] == "none"
    assert decks.store.source(src["id"])["text"] == "Photosynthesis makes sugar."
    assert decks.gemini.requests == []
    assert not any(dict(r.url.params).get("mimeType") == "application/pdf" for r in decks.requests)


def test_a_deck_over_the_cap_stays_text_only_and_failed(client, decks):  # noqa: F811
    decks.meta["mimeType"] = drive.SLIDES
    decks.pdf = httpx.Response(200, content=b"%" * 64, headers={"content-length": str(drive.PDF_BYTES + 1)})
    src = client.post("/study/drive/add", json={"file_id": FILE_ID}).json()["source"]
    assert src["deck_status"] == "failed" and src["pages"] == 0 and src["chars"] > 0
    assert decks.gemini.requests == []


def test_a_gemini_failure_keeps_the_text_source(client, decks, monkeypatch):  # noqa: F811
    decks.meta["mimeType"] = drive.DOC
    decks.gemini.responses = [httpx.Response(500)]
    got = client.post("/study/drive/add", json={"file_id": FILE_ID})
    assert got.status_code == 200
    assert got.json()["source"]["deck_status"] == "failed" and len(decks.gemini.requests) == 2


def test_googles_pdf_export_limit_keeps_the_text_source(client, decks):  # noqa: F811
    decks.meta["mimeType"] = drive.SLIDES
    decks.pdf = httpx.Response(403, json={"error": {"code": 403, "errors": [{"reason": "exportSizeLimitExceeded"}]}})
    src = client.post("/study/drive/add", json={"file_id": FILE_ID}).json()["source"]
    assert src["deck_status"] == "failed" and decks.gemini.requests == []


def test_pdfs_are_listed(client, google):  # noqa: F811
    google.files = [{"id": FILE_ID, "name": "Week 5 reading.pdf", "mimeType": drive.PDF,
                     "modifiedTime": "2026-11-01T10:00:00Z"},
                    {"id": "1ZzZzZzZzZzZ", "name": "Photo", "mimeType": "image/png"}]
    files = client.get("/study/drive/files").json()["files"]
    assert [(f["name"], f["kind"]) for f in files] == [("Week 5 reading.pdf", "pdf")]
    assert f"mimeType='{drive.PDF}'" in dict(google.requests[-1].url.params)["q"]
