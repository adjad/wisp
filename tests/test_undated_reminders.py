"""Incomplete Apple reminders with no due date: visible to reminder search, invisible to schedules.

The native sync used to keep only reminders with a due date, so a person whose live
reminders carry no date saw "Apple Reminders incomplete-item matches: None". Undated
reminders now travel as a separate list into their own table. Nothing time-based
(notifications, overdue, upcoming, history) may ever see them.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-undated-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ["HOME"] = _scratch.name

from fastapi.testclient import TestClient  # noqa: E402

import service.main as main  # noqa: E402
from service.assistant import sync_status  # noqa: E402
from service.assistant.reminders import due_reminders  # noqa: E402
from service.assistant.store import AssistantStore  # noqa: E402
from service.tools import assistant_tools  # noqa: E402

READY = {"sources": [{"id": "reminders", "state": "ready"}], "reminders_fresh": True}
_clock = iter(range(1, 10_000))


def _auth():
    """Each authoritative read carries a newer snapshot time, as the app's do."""
    return {"authorized": True, "count": 0, "snapshot_started_at": float(next(_clock))}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(main, "assistant_store", s)
    monkeypatch.setattr(assistant_tools, "assistant_store", s)
    return s


def _item(source_id, title, context="Reminders"):
    return {"source_id": source_id, "title": title, "context": context}


def _post(body):
    return TestClient(main.app).post("/assistant/sync/calendar", json=body)


def _search(query="", scope="all", readiness=READY):
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=readiness):
        return asyncio.run(assistant_tools.search_reminders(query, scope))


# --- store -------------------------------------------------------------------

def test_replace_is_a_full_replacement(store):
    assert store.replace_undated_reminders([_item("a", "Zybook"), _item("b", "Library card")]) == 2
    assert [r["title"] for r in store.undated_reminders()] == ["Library card", "Zybook"]
    store.replace_undated_reminders([_item("b", "Library card")])
    assert [r["source_id"] for r in store.undated_reminders()] == ["b"]
    store.replace_undated_reminders([])
    assert store.undated_reminders() == []


def test_replace_skips_untitled_and_dedupes_by_id(store):
    n = store.replace_undated_reminders([_item("a", "First"), _item("a", "Renamed"),
                                         _item("", "No id"), _item("c", "   ")])
    assert n == 1
    assert [r["title"] for r in store.undated_reminders()] == ["Renamed"]


def test_query_filters_by_title(store):
    store.replace_undated_reminders([_item("a", "Work on the Zybook"), _item("b", "Get a library card")])
    assert [r["title"] for r in store.undated_reminders("zybook")] == ["Work on the Zybook"]


def test_undated_reminders_never_reach_time_based_reads(store):
    now = time.time()
    store.replace_undated_reminders([_item("a", "Work on the Zybook")])
    assert store.upcoming(now, days=60) == []
    assert store.history(now=now, days=3650) == []
    assert store.active_between() == []
    assert store.next_active(now) is None
    assert store.active_future(now, horizon_days=3650) == []
    assert due_reminders(store, now) == []


# --- endpoint ----------------------------------------------------------------

def test_authoritative_post_stores_the_list_and_keeps_dated_sync(store):
    when = time.time() + 3600
    r = _post({"source": "reminders", "diagnostics": _auth(),
               "events": [{"source_id": "d", "kind": "reminder", "title": "Dated", "when_ts": when}],
               "undated": [_item("a", "Work on the Zybook")]})
    assert r.status_code == 200 and r.json()["synced"] == 1
    assert [x["title"] for x in store.undated_reminders()] == ["Work on the Zybook"]
    assert [c["title"] for c in store.upcoming(time.time(), days=2)] == ["Dated"]


def test_a_missing_key_keeps_the_last_good_list(store):
    _post({"source": "reminders", "diagnostics": _auth(), "events": [], "undated": [_item("a", "Keep me")]})
    _post({"source": "reminders", "diagnostics": _auth(), "events": []})
    assert [x["title"] for x in store.undated_reminders()] == ["Keep me"]


def test_an_empty_list_clears_it(store):
    _post({"source": "reminders", "diagnostics": _auth(), "events": [], "undated": [_item("a", "Gone soon")]})
    _post({"source": "reminders", "diagnostics": _auth(), "events": [], "undated": []})
    assert store.undated_reminders() == []


@pytest.mark.parametrize("diagnostics", [
    {"authorized": False, "syncing": False},
    {"authorized": True, "available": False, "reason": "EventKit reported no reminder lists"},
    {"syncing": True},
])
def test_an_unavailable_read_never_touches_the_list(store, diagnostics):
    _post({"source": "reminders", "diagnostics": _auth(), "events": [], "undated": [_item("a", "Keep me")]})
    _post({"source": "reminders", "diagnostics": diagnostics, "events": [],
           "undated": [_item("x", "Must be ignored")]})
    assert [x["title"] for x in store.undated_reminders()] == ["Keep me"]


def test_calendar_posts_cannot_write_the_list(store):
    _post({"source": "calendar", "diagnostics": {"authorized": True}, "events": [],
           "undated": [_item("a", "Not a reminder")]})
    assert store.undated_reminders() == []


def test_the_list_is_bounded(store):
    _post({"source": "reminders", "diagnostics": _auth(), "events": [],
           "undated": [_item(f"id{i}", f"Reminder {i}") for i in range(500)]})
    assert len(store.undated_reminders()) == 200


# --- what the chat tool says ---------------------------------------------------

def test_scope_all_lists_undated_reminders_instead_of_none(store):
    store.replace_undated_reminders([_item("a", "Work on the Zybook"), _item("b", "Get a library card")])
    answer = _search()
    assert "Apple Reminders with no due date (incomplete):" in answer
    assert "- Work on the Zybook (Reminders)" in answer and "- Get a library card (Reminders)" in answer
    assert "None with a due date." in answer
    assert "no active match" not in answer


def test_a_narrow_scope_counts_them_instead_of_hiding_them(store):
    store.replace_undated_reminders([_item("a", "Work on the Zybook"), _item("b", "Get a library card")])
    answer = _search(scope="today")
    assert "2 incomplete Apple reminders with no due date are not in this time window" in answer
    assert "Work on the Zybook" not in answer


def test_a_query_narrows_the_undated_list(store):
    store.replace_undated_reminders([_item("a", "Work on the Zybook"), _item("b", "Get a library card")])
    answer = _search("library")
    assert "Get a library card" in answer and "Zybook" not in answer


def test_nothing_anywhere_still_says_no_match(store):
    assert "no active match" in _search("anything")


def test_undated_are_withheld_when_reminders_cannot_be_verified(store):
    store.replace_undated_reminders([_item("a", "Work on the Zybook")])
    for readiness in ({"sources": [{"id": "reminders", "state": "unavailable"}]},
                      {"sources": [{"id": "reminders", "state": "ready"}], "reminders_fresh": False},
                      {"sources": [{"id": "reminders", "state": "syncing"}], "reminders_fresh": True}):
        answer = _search(readiness=readiness)
        assert "Work on the Zybook" not in answer
        assert "no due date" not in answer


def test_a_replayed_snapshot_cannot_overwrite_a_newer_list(store):
    fresh = _auth()
    assert _post({"source": "reminders", "diagnostics": fresh, "events": [],
                  "undated": [_item("a", "Newest")]}).status_code == 200
    older = dict(fresh)
    assert _post({"source": "reminders", "diagnostics": older, "events": [],
                  "undated": [_item("z", "Replayed")]}).status_code == 409
    assert [x["title"] for x in store.undated_reminders()] == ["Newest"]
