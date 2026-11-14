"""Study's Google Drive routes (service/study/drive.py, service/study/drive_api.py): the account
choice, listing with an escaped query, exporting one file as a source, every refusal and the
native guard.

Google is an httpx.MockTransport and the sign-in is a fake gmail_source; no network, no
keychain, no ~/.moe. The store's save_source is faked when the store does not have it yet."""
from __future__ import annotations

import contextlib
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import gmail_source
from service.study import api, drive, drive_api
from service.study.store import StudyStore

JSON = {"content-type": "application/json"}
DRIVE = drive.DRIVE_SCOPE
FILE_ID = "1AbCdEfGhIjKlMnOp_-9"


class Google:
    """Fake Drive: records requests, answers from a table."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.files: list[dict] = []
        self.meta: dict = {"id": FILE_ID, "name": "Lab notes", "mimeType": drive.DOC,
                           "webViewLink": f"https://docs.google.com/document/d/{FILE_ID}/edit"}
        self.export: httpx.Response | None = None
        self.chunks_sent = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.scheme == "https" and request.url.host == "www.googleapis.com"
        assert request.headers["authorization"] == "Bearer test-token"
        path = request.url.path
        if path == "/drive/v3/files":
            return httpx.Response(200, json={"files": self.files})
        if path == f"/drive/v3/files/{FILE_ID}":
            return httpx.Response(200, json=self.meta)
        if path == f"/drive/v3/files/{FILE_ID}/export":
            return self.export or httpx.Response(200, text="Photosynthesis makes sugar.")
        return httpx.Response(404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}})


class FakeStore:
    """The real StudyStore plus save_source when the base store lacks it."""

    def __init__(self, real: StudyStore):
        self.real = real
        self.saved: list[dict] = []

    def __getattr__(self, name):
        return getattr(self.real, name)

    def save_source(self, source: dict) -> dict:
        if hasattr(self.real, "save_source"):
            summary = self.real.save_source(source)
        else:
            summary = {"id": f"src_{len(self.saved):012x}", "course_id": source.get("course_id"),
                       "kind": source["kind"], "title": source["title"], "url": source["url"],
                       "chars": len(source["text"]), "created_at": 1.0}
        self.saved.append(source)
        return summary


@pytest.fixture
def google(tmp_path, monkeypatch):
    g = Google()
    g.accounts = {"student@example.com": {"refresh_token": "r", "client_id": "c", "scopes": [
        gmail_source.SCOPE, DRIVE]}}
    monkeypatch.setenv("WISP_HOME", str(tmp_path))
    monkeypatch.setattr(gmail_source, "connected_accounts", lambda cfg=None: dict(g.accounts))

    @contextlib.contextmanager
    def bound(account):
        assert account in g.accounts
        g.bound = account
        yield
    monkeypatch.setattr(gmail_source, "bound_account", bound)
    monkeypatch.setattr(gmail_source, "still_connected", lambda: True)

    async def token(http):
        return "test-token"
    monkeypatch.setattr(gmail_source, "access_token", token)
    monkeypatch.setattr(drive, "_transport", httpx.MockTransport(g.handler))

    store = FakeStore(StudyStore(tmp_path / "study" / "study.db"))
    g.store = store
    for module in (api, drive_api):
        monkeypatch.setattr(module, "study_store", lambda: store)
        monkeypatch.setattr(module, "study_exists", lambda: True)

    async def connector_state():
        return {"provider": "gemini", "model_id": "gemini-3.8-flash", "ready": True}
    monkeypatch.setattr(api.connector, "status_async", connector_state)
    return g


@pytest.fixture
def client(google):
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(drive_api.router)
    # The app sends JSON Content-Type on every request, GETs included (native_only).
    return TestClient(app, headers=JSON)


def add(client, **body):
    return client.post("/study/drive/add", json={"file_id": FILE_ID} | body)


# --- status --------------------------------------------------------------------------

def test_status_without_an_account(client, google):
    google.accounts = {}
    got = client.get("/study/drive/status").json()
    assert got == {"connected": False, "drive": False, "account": None, "message": drive.NOT_CONNECTED}


def test_status_without_the_drive_scope(client, google):
    google.accounts = {"a@example.com": {"refresh_token": "r", "client_id": "c", "scopes": [gmail_source.SCOPE]}}
    got = client.get("/study/drive/status").json()
    assert got["connected"] and not got["drive"] and got["account"] == "a@example.com"
    assert "Reconnect Google in Settings" in got["message"]


def test_status_picks_the_first_account_with_drive(client, google):
    google.accounts = {
        "mail-only@example.com": {"refresh_token": "r", "client_id": "c", "scopes": [gmail_source.SCOPE]},
        "expired@example.com": {"refresh_token": "r", "client_id": "c", "scopes": [DRIVE], "auth_expired": 1},
        "school@example.com": {"refresh_token": "r", "client_id": "c", "scopes": [DRIVE]},
    }
    got = client.get("/study/drive/status").json()
    assert got == {"connected": True, "drive": True, "account": "school@example.com", "message": None}


# --- listing -------------------------------------------------------------------------

def test_listing_maps_kinds_and_dates(client, google):
    google.files = [
        {"id": "doc_id_0123456", "name": "Essay", "mimeType": drive.DOC,
         "modifiedTime": "2026-11-10T12:00:00.000Z", "webViewLink": "https://docs.google.com/document/d/x"},
        {"id": "slides_id_0123", "name": "Deck", "mimeType": drive.SLIDES, "modifiedTime": "garbage"},
        {"id": "sheet_id_01234", "name": "Sheet", "mimeType": "application/vnd.google-apps.spreadsheet"},
        {"id": "bad id!", "name": "Odd", "mimeType": drive.DOC},
    ]
    got = client.get("/study/drive/files").json()["files"]
    assert got == [
        {"id": "doc_id_0123456", "name": "Essay", "kind": "doc", "modified": 1794312000.0,
         "url": "https://docs.google.com/document/d/x"},
        {"id": "slides_id_0123", "name": "Deck", "kind": "slides", "modified": None, "url": None},
    ]
    params = google.requests[0].url.params
    assert params["q"] == drive.drive_query("")
    assert params["orderBy"] == "modifiedTime desc" and params["pageSize"] == "25"
    assert params["fields"] == "files(id,name,mimeType,modifiedTime,webViewLink)"


def test_the_search_word_is_escaped(client, google):
    client.get("/study/drive/files", params={"q": "it's a\\b", "limit": 5})
    params = google.requests[0].url.params
    assert params["q"].endswith(" and name contains 'it\\'s a\\\\b'")
    assert params["q"].startswith(f"(mimeType='{drive.DOC}' or mimeType='{drive.SLIDES}') and trashed=false")
    assert params["pageSize"] == "5"


def test_escaping_drops_control_characters():
    assert drive.escape("a'\n) or name contains ('x") == "a\\' ) or name contains (\\'x"
    assert drive.drive_query("   ") == drive.drive_query("")


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 26}, {"limit": "x"}, {"q": "a" * 201}])
def test_bad_listing_parameters_are_422(client, google, params):
    got = client.get("/study/drive/files", params=params)
    assert got.status_code == 422 and got.json()["detail"] == "invalid"
    assert google.requests == []


def test_listing_without_drive_is_409(client, google):
    google.accounts = {"a@example.com": {"refresh_token": "r", "client_id": "c", "scopes": [gmail_source.SCOPE]}}
    got = client.get("/study/drive/files")
    assert got.status_code == 409 and got.json()["detail"] == "google_missing"
    assert google.requests == []


# --- adding --------------------------------------------------------------------------

def test_adding_a_doc_saves_a_source(client, google):
    google.store.upsert_courses([{"id": "canvas:1", "name": "Biology", "source": "canvas"}])
    got = add(client, course_id="canvas:1")
    assert got.status_code == 200
    body = got.json()
    assert body["source"]["kind"] == "drive" and body["source"]["title"] == "Lab notes"
    assert "courses" in body["state"]
    assert google.store.saved == [{"kind": "drive", "url": google.meta["webViewLink"], "title": "Lab notes",
                                   "text": "Photosynthesis makes sugar.", "course_id": "canvas:1"}]
    export = google.requests[-1]
    assert export.url.path == f"/drive/v3/files/{FILE_ID}/export"
    assert export.url.params["mimeType"] == "text/plain"


def test_slides_without_a_link_get_the_slides_url(client, google):
    google.meta = {"id": FILE_ID, "name": "Deck", "mimeType": drive.SLIDES}
    assert add(client).status_code == 200
    assert google.store.saved[0]["url"] == f"https://docs.google.com/presentation/d/{FILE_ID}/edit"
    assert google.store.saved[0]["course_id"] is None


def test_a_long_export_stops_at_the_cap(client, google, monkeypatch):
    monkeypatch.setattr(drive, "SOURCE_CHARS", 1000)

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                google.chunks_sent += 1
                yield b"x" * 300

    google.export = httpx.Response(200, stream=Endless(), headers={"content-type": "text/plain"})
    assert add(client).status_code == 200
    assert len(google.store.saved[0]["text"]) == 1000
    assert google.chunks_sent < 10


def test_googles_export_limit_is_too_large(client, google):
    google.export = httpx.Response(403, json={"error": {"code": 403, "message": "too big", "errors": [
        {"reason": "exportSizeLimitExceeded"}]}})
    got = add(client)
    assert got.status_code == 413 and got.json()["detail"] == "too_large"
    assert google.store.saved == []


@pytest.mark.parametrize("status,reason,code,http", [
    (403, "insufficientPermissions", "google_missing", 409), (401, "authError", "google_missing", 409),
    (403, "accessNotConfigured", "google_error", 502), (500, "backendError", "google_error", 502),
    (404, "notFound", "not_found", 404),
])
def test_export_failures_map_to_refusals(client, google, status, reason, code, http):
    google.export = httpx.Response(status, json={"error": {"errors": [{"reason": reason}]}})
    got = add(client)
    assert got.status_code == http and got.json()["detail"] == code
    assert "message" in got.json() and google.store.saved == []


def test_a_file_that_is_not_a_doc_or_slides_is_refused(client, google):
    google.meta = {"id": FILE_ID, "name": "Sheet", "mimeType": "application/vnd.google-apps.spreadsheet"}
    got = add(client)
    assert got.status_code == 422 and got.json()["detail"] == "invalid"
    assert all(not r.url.path.endswith("/export") for r in google.requests)


def test_an_empty_export_is_refused(client, google):
    google.export = httpx.Response(200, text="﻿  \n")
    assert add(client).status_code == 422 and google.store.saved == []


def test_a_timeout_is_a_google_error(client, google):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    drive._transport = httpx.MockTransport(slow)
    got = add(client)
    assert got.status_code == 502 and got.json()["detail"] == "google_error"


@pytest.mark.parametrize("file_id", ["short", "../../etc/passwd_x", "a" * 201, "abc def ghij", "abcdefghij?x=1"])
def test_a_bad_file_id_is_422(client, google, file_id):
    got = client.post("/study/drive/add", json={"file_id": file_id})
    assert got.status_code == 422 and got.json()["detail"] == "invalid"
    assert google.requests == []


@pytest.mark.parametrize("body", [{}, {"file_id": 5}, {"file_id": FILE_ID, "extra": 1}])
def test_a_bad_body_is_422(client, google, body):
    got = client.post("/study/drive/add", json=body)
    assert got.status_code == 422 and got.json()["detail"] == "invalid"


def test_an_unknown_course_is_404(client, google):
    got = add(client, course_id="canvas:404")
    assert got.status_code == 404 and got.json()["detail"] == "not_found"
    assert google.requests == []


def test_without_a_google_account_adding_is_409(client, google):
    google.accounts = {}
    got = add(client)
    assert got.status_code == 409 and got.json()["detail"] == "google_missing"
    assert "Settings" in got.json()["message"] and google.requests == []


def test_no_token_or_text_is_printed(client, google, capsys):
    add(client)
    out = capsys.readouterr()
    assert "test-token" not in out.out + out.err and "Photosynthesis" not in out.out + out.err


# --- the native guard ----------------------------------------------------------------

def test_adding_is_native_only(client, google):
    from_page = client.post("/study/drive/add", json={"file_id": FILE_ID}, headers={"origin": "https://evil.example"})
    assert from_page.status_code == 403
    as_form = client.post("/study/drive/add", content=json.dumps({"file_id": FILE_ID}).encode(),
                          headers={"content-type": "text/plain"})
    assert as_form.status_code == 415
    assert google.requests == [] and google.store.saved == []


def test_drive_reads_refuse_a_web_page(client, google):
    for path in ("/study/drive/status", "/study/drive/files"):
        assert client.get(path, headers={"origin": "https://evil.example"}).status_code == 403
        assert client.get(path, headers={"content-type": "text/plain"}).status_code == 415
    assert google.requests == []
