"""Study blocks to Calendar (service/study/blocks_api.py) and the Day marks (day_hook).

The Day snapshot, the Calendar bridge and its cancel are faked; no network, no app, no ~/.moe."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.study import blocks_api
from service.study.store import StudyStore

JSON = {"content-type": "application/json"}
DAY, TZ = "2026-11-20", "America/Los_Angeles"
BID = "study-block:asg-1"
BLOCK = {"id": BID, "title": "Study: Problem set 4", "start": 1_800_000_000.0, "end": 1_800_002_700.0,
         "kind": "study", "task_id": None, "warnings": [], "added": False, "event_source_id": None}


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = StudyStore(tmp_path / "study.db")
    monkeypatch.setattr(blocks_api, "study_store", lambda: store)
    monkeypatch.setattr(blocks_api, "study_exists", lambda: True)
    calls = {"create": [], "delete": [], "blocks": [dict(BLOCK)]}
    monkeypatch.setattr(blocks_api, "_snapshot", lambda day, tz: {"blocks": calls["blocks"]})

    async def create(plan):
        calls["create"].append(plan)
        return calls.get("create_result") or {"ok": True, "status": "succeeded", "source_id": "EVT-1"}

    async def delete(record):
        calls["delete"].append(record)
        return calls.get("delete_result") or {"ok": True, "status": "succeeded"}

    monkeypatch.setattr(blocks_api, "_create", create)
    monkeypatch.setattr(blocks_api, "_delete", delete)
    monkeypatch.setattr(blocks_api, "_calendar_state", lambda day, tz: (None, {}))   # never the real store
    app = FastAPI()
    app.include_router(blocks_api.router)
    return TestClient(app, headers=JSON), store, calls


def test_add_books_exactly_the_shown_block_and_records_it(env):
    client, store, calls = env
    got = client.post(f"/study/blocks/{BID}/add", json={"day": DAY, "timezone": TZ}).json()
    assert got == {"ok": True, "status": "succeeded", "block_id": BID, "source_id": "EVT-1"}
    plan = calls["create"][0]
    assert plan["title"] == "Study: Problem set 4" and plan["start_ts"] == BLOCK["start"]
    assert plan["capture_key"] == f"capture:study:{BID}:{DAY}"     # a moved retry can't book twice
    assert plan["duration_min"] == 45 and plan["automatic"] is False and plan["day"] == DAY
    assert store.meta(blocks_api.META)[BID]["source_id"] == "EVT-1"
    again = client.post(f"/study/blocks/{BID}/add", json={"day": DAY, "timezone": TZ}).json()
    assert again["ok"] is True and len(calls["create"]) == 1     # never booked twice


def test_a_block_no_longer_shown_is_refused(env):
    client, store, calls = env
    calls["blocks"] = []
    got = client.post(f"/study/blocks/{BID}/add", json={"day": DAY, "timezone": TZ}).json()
    assert got["ok"] is False and got["error"] == blocks_api.GONE and not calls["create"]


def test_a_failed_booking_records_nothing(env):
    client, store, calls = env
    calls["create_result"] = {"ok": False, "status": "blocked", "error": "View-only mode"}
    got = client.post(f"/study/blocks/{BID}/add", json={"day": DAY, "timezone": TZ}).json()
    assert got == {"ok": False, "status": "blocked", "error": "View-only mode"}
    assert not store.meta(blocks_api.META, {})


@pytest.mark.parametrize("body", [{}, {"day": "tomorrow", "timezone": TZ}, {"day": DAY, "timezone": "Nowhere/X"},
                                  {"day": DAY, "timezone": TZ, "title": "x"}])
def test_bad_bodies_are_refused(env, body):
    client, _, calls = env
    assert client.post(f"/study/blocks/{BID}/add", json=body).json()["ok"] is False and not calls["create"]


def test_only_study_block_ids(env):
    client, _, calls = env
    got = client.post("/study/blocks/cal-123/add", json={"day": DAY, "timezone": TZ}).json()
    assert got["ok"] is False and not calls["create"]


def test_a_web_page_cannot_book(env):
    client, _, calls = env
    got = client.post(f"/study/blocks/{BID}/add", json={"day": DAY, "timezone": TZ},
                      headers={"origin": "https://evil.example"})
    assert got.status_code >= 400 and not calls["create"]


def test_undo_cancels_the_recorded_event_and_forgets_it(env):
    client, store, calls = env
    client.post(f"/study/blocks/{BID}/add", json={"day": DAY, "timezone": TZ})
    got = client.post(f"/study/blocks/{BID}/undo", json={}).json()
    assert got == {"ok": True, "status": "succeeded", "block_id": BID}
    assert calls["delete"][0]["source_id"] == "EVT-1" and calls["delete"][0]["start"] == BLOCK["start"]
    assert BID not in store.meta(blocks_api.META, {})


def test_undo_without_a_booking_or_failed_cancel(env):
    client, store, calls = env
    assert client.post(f"/study/blocks/{BID}/undo", json={}).json()["error"] == blocks_api.NOT_ADDED
    client.post(f"/study/blocks/{BID}/add", json={"day": DAY, "timezone": TZ})
    calls["delete_result"] = {"ok": False, "status": "failed", "error": "Changed in Calendar"}
    assert client.post(f"/study/blocks/{BID}/undo", json={}).json()["ok"] is False
    assert BID in store.meta(blocks_api.META)                    # still Wisp's to undo


def test_mark_keeps_a_booked_block_and_drops_its_calendar_copy():
    moved = {**BLOCK, "start": BLOCK["start"] + 3600, "end": BLOCK["end"] + 3600}
    other = {**BLOCK, "id": "study-block:asg-2", "title": "Study: Essay"}
    copy = {"id": "cal-9", "title": "Study: Problem set 4", "start": BLOCK["start"], "end": BLOCK["end"],
            "kind": "calendar"}
    meeting = {"id": "cal-1", "title": "Lab", "start": 1_799_990_000.0, "end": 1_799_993_600.0, "kind": "calendar"}
    booked = {BID: {"source_id": "EVT-1", "title": BLOCK["title"], "start": BLOCK["start"], "end": BLOCK["end"],
                    "day": DAY}, "study-block:old": {"source_id": "E0", "title": "x", "start": 1.0, "end": 2.0,
                                                       "day": "2026-11-19"}}
    blocks, others = blocks_api.mark([moved, other], [copy, meeting], DAY, booked)
    by = {b["id"]: b for b in blocks}
    assert by[BID]["added"] is True and by[BID]["event_source_id"] == "EVT-1" and by[BID]["start"] == BLOCK["start"]
    assert by["study-block:asg-2"]["added"] is False and by["study-block:asg-2"]["event_source_id"] is None
    assert len(blocks) == 2 and others == [meeting]


def test_deck_error_on_failed_sources(tmp_path):
    store = StudyStore(tmp_path / "study.db")
    s = store.save_source({"kind": "drive", "url": "https://docs.google.com/x", "title": "Lecture 2", "text": "t"})
    store.set_deck_status(s["id"], "failed")
    assert store.sources()[0]["deck_error"] == "Wisp couldn't read the slides."
    store.set_deck_error(s["id"], "The file is over 14 MB, too large to read as slides.")
    assert store.sources()[0]["deck_error"].startswith("The file is over 14 MB")
    store.set_deck_status(s["id"], "none")
    assert "deck_error" not in store.sources()[0]


def test_add_and_undo_are_audited(env, monkeypatch):
    client, _, _ = env
    seen = []
    monkeypatch.setattr(blocks_api, "_audit", lambda event, **f: seen.append((event, f["ok"])))
    client.post(f"/study/blocks/{BID}/add", json={"day": DAY, "timezone": TZ})
    client.post(f"/study/blocks/{BID}/undo", json={})
    assert seen == [("study_block_calendar_add", True), ("study_block_calendar_undo", True)]


# --- edited and deleted in Calendar -------------------------------------------------------

def _rec(**kw):
    return {"source_id": "EVT-1", "title": BLOCK["title"], "start": BLOCK["start"], "end": BLOCK["end"],
            "day": DAY, "timezone": TZ, "at": 1000.0, **kw}


FRESH = {"available": True, "syncing": False, "last_sync": 2000.0}


def test_gone_only_after_a_fresh_sync_well_after_the_booking():
    assert blocks_api.gone(_rec(), FRESH, {}, now=2010.0) is True
    assert blocks_api.gone(_rec(), FRESH, {"EVT-1": (1, 2, "x")}, now=2010.0) is False
    assert blocks_api.gone(_rec(at=1990.0), FRESH, {}, now=2010.0) is False          # not settled yet
    assert blocks_api.gone(_rec(), {**FRESH, "syncing": True}, {}, now=2010.0) is False
    assert blocks_api.gone(_rec(), FRESH, {}, now=2500.0) is False                    # sync too old
    assert blocks_api.gone(_rec(), None, {}, now=2010.0) is False                     # unknown


def test_a_moved_event_with_a_new_identifier_is_not_gone():
    same = {"EVT-9": (BLOCK["start"], BLOCK["end"], BLOCK["title"])}
    assert blocks_api.gone(_rec(), FRESH, same, now=2010.0) is False
    other = {"EVT-9": (BLOCK["start"] + 600, BLOCK["end"] + 600, BLOCK["title"])}
    assert blocks_api.gone(_rec(), FRESH, other, now=2010.0) is True


def test_a_booking_without_its_timezone_is_never_judged_gone():
    rec = _rec()
    rec.pop("timezone")
    assert blocks_api.gone(rec, FRESH, {}, now=2010.0) is False


def test_edited_notices_a_changed_end_start_or_title():
    same = {"EVT-1": (BLOCK["start"], BLOCK["end"], BLOCK["title"])}
    assert blocks_api.edited(_rec(), same) is False
    assert blocks_api.edited(_rec(), {"EVT-1": (BLOCK["start"], BLOCK["end"] + 900, BLOCK["title"])}) is True
    assert blocks_api.edited(_rec(), {"EVT-1": (BLOCK["start"] + 600, BLOCK["end"], BLOCK["title"])}) is True
    assert blocks_api.edited(_rec(), {"EVT-1": (BLOCK["start"], BLOCK["end"], "Renamed")}) is True
    assert blocks_api.edited(_rec(), {}) is False


def _booked(env, monkeypatch, status, events):
    client, store, calls = env
    client.post(f"/study/blocks/{BID}/add", json={"day": DAY, "timezone": TZ})
    record = dict(store.meta(blocks_api.META)[BID], at=1000.0)
    store.set_meta(blocks_api.META, {BID: record})
    monkeypatch.setattr(blocks_api, "_calendar_state", lambda day, tz: (status, events))
    monkeypatch.setattr(blocks_api.time, "time", lambda: 2010.0)
    return client, store, calls


def test_undo_refuses_an_event_edited_in_calendar(env, monkeypatch):
    client, store, calls = _booked(env, monkeypatch, FRESH,
                                   {"EVT-1": (BLOCK["start"], BLOCK["end"] + 900, BLOCK["title"])})
    got = client.post(f"/study/blocks/{BID}/undo", json={}).json()
    assert got == {"ok": False, "status": "changed", "error": blocks_api.CHANGED}
    assert not calls["delete"] and BID in store.meta(blocks_api.META)


def test_undo_of_an_event_deleted_in_calendar_just_forgets_it(env, monkeypatch):
    client, store, calls = _booked(env, monkeypatch, FRESH, {})
    got = client.post(f"/study/blocks/{BID}/undo", json={}).json()
    assert got == {"ok": True, "status": "gone", "block_id": BID}
    assert not calls["delete"] and BID not in store.meta(blocks_api.META, {})


def test_after_a_deletion_adding_again_books_with_a_new_key(env, monkeypatch):
    client, store, calls = _booked(env, monkeypatch, FRESH, {})
    client.post(f"/study/blocks/{BID}/undo", json={})
    client.post(f"/study/blocks/{BID}/add", json={"day": DAY, "timezone": TZ})
    keys = [p["capture_key"] for p in calls["create"]]
    assert keys == [f"capture:study:{BID}:{DAY}", f"capture:study:{BID}:{DAY}:1"]


def test_unchanged_event_still_undoes_through_the_bridge(env, monkeypatch):
    client, store, calls = _booked(env, monkeypatch, FRESH, {"EVT-1": (BLOCK["start"], BLOCK["end"], BLOCK["title"])})
    assert client.post(f"/study/blocks/{BID}/undo", json={}).json()["status"] == "succeeded" and calls["delete"]


def test_day_stops_showing_a_block_deleted_in_calendar():
    forgot = []
    booked = {BID: _rec()}
    blocks, _ = blocks_api.mark([dict(BLOCK)], [], DAY, booked, status=FRESH, events={}, now=2010.0,
                                forget=lambda b, d: forgot.append((b, d)))
    assert [b["added"] for b in blocks] == [False] and forgot == [(BID, DAY)]
    blocks, _ = blocks_api.mark([dict(BLOCK)], [], DAY, booked, status=FRESH,
                                events={"EVT-1": (BLOCK["start"], BLOCK["end"], BLOCK["title"])}, now=2010.0)
    assert [b["added"] for b in blocks] == [True]
    # a stale or unknown Calendar copy concludes nothing
    blocks, _ = blocks_api.mark([dict(BLOCK)], [], DAY, booked, status=None, events={}, now=2010.0)
    assert [b["added"] for b in blocks] == [True]
