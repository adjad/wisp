"""Incomplete Apple reminders with no due date: visible to reminder search, invisible to schedules.

The native sync used to keep only reminders with a due date, so a person whose live
reminders carry no date saw "Apple Reminders incomplete-item matches: None". Undated
reminders now travel with the same snapshot as the dated ones.

R1  The dated rows, the source receipt and the undated list commit in ONE transaction:
    a failure anywhere rolls all of them back and the equal-timestamp retry is valid again.
R4  The undated list carries its own freshness and completeness. It is believed only when
    it came from the snapshot the receipt records as latest; a missing, invalid, older or
    truncated list is never presented as fresh or exhaustive.

Nothing time-based (notifications, overdue, upcoming, history) may ever see these rows.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
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
from service.assistant.store import (AssistantStore, UNDATED_MAX,  # noqa: E402
                                     parse_undated_snapshot)
from service.tools import assistant_tools  # noqa: E402

READY = {"sources": [{"id": "reminders", "state": "ready"}], "reminders_fresh": True}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    s._test_path = tmp_path / "assistant.db"
    monkeypatch.setattr(main, "assistant_store", s)
    monkeypatch.setattr(assistant_tools, "assistant_store", s)
    return s


_clock = iter(range(100, 100_000))


def _ts() -> float:
    """Each authoritative read carries a newer snapshot time, as the app's do."""
    return float(next(_clock))


def _diag(ts: float | None = None) -> dict:
    return {"authorized": True, "count": 0, "snapshot_started_at": _ts() if ts is None else ts}


def _item(source_id, title, context="Reminders"):
    return {"source_id": source_id, "title": title, "context": context}


def _dated(source_id, title, when=None):
    return {"source_id": source_id, "kind": "reminder", "title": title,
            "when_ts": (time.time() + 3600) if when is None else when}


_UNSET = object()


def _body(undated=_UNSET, total=_UNSET, ts=None, events=()):
    body = {"source": "reminders", "diagnostics": _diag(ts), "events": list(events)}
    if undated is not _UNSET:
        body["undated"] = undated
        body["undated_total"] = (len(undated) if isinstance(undated, list) else 0) \
            if total is _UNSET else total
    elif total is not _UNSET:
        body["undated_total"] = total
    return body


def _post(body, **client_kwargs):
    return TestClient(main.app, **client_kwargs).post("/assistant/sync/calendar", json=body)


def _search(query="", scope="all", readiness=READY):
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=readiness):
        return asyncio.run(assistant_tools.search_reminders(query, scope))


def _titles(store):
    return sorted(r["title"] for r in store.undated_reminders())


NOT_INCLUDED = "Reminders without a due date were not included in the latest read"


# --- parsing --------------------------------------------------------------------

@pytest.mark.parametrize("raw,total", [
    (None, 0), ({"a": 1}, 1), ("text", 1), (5, 5), ([], None), ([], True), ([], "0"), ([], -1),
    ([_item("a", "x"), _item("b", "y")], 1),                 # total below the list
    ([_item("a", "x"), "not an object"], 2),
    ([_item("", "x")], 1), ([_item("   ", "x")], 1), ([_item(None, "x")], 1), ([_item(7, "x")], 1),
    ([{"source_id": "a", "title": None}], 1), ([{"source_id": "a", "title": 5}], 1),
    ([{"source_id": "a", "title": "x", "context": 5}], 1),
])
def test_invalid_snapshots_are_rejected_whole(raw, total):
    assert parse_undated_snapshot(raw, total) is None


def test_a_valid_snapshot_is_normalised():
    snap = parse_undated_snapshot([_item(" a ", " First "), _item("a", "Renamed"),
                                   {"source_id": "b", "title": "   ", "context": None}], 3)
    assert [(r[0], r[1], r[2]) for r in snap.rows] == [("a", "Renamed", "Reminders"), ("b", "(untitled)", None)]
    assert snap.total == 3 and snap.truncated is False   # a duplicate id counts once, total stays honest


def test_total_above_the_list_marks_it_truncated_and_over_the_cap_is_cut():
    snap = parse_undated_snapshot([_item("a", "x")], 5)
    assert snap.truncated is True and snap.total == 5 and len(snap.rows) == 1
    big = parse_undated_snapshot([_item(f"id{i}", f"r{i}") for i in range(300)], 300)
    assert len(big.rows) == UNDATED_MAX and big.truncated is True and big.total == 300


# --- store ----------------------------------------------------------------------

def test_replace_wrapper_never_reads_as_current(store):
    store.replace_undated_reminders([_item("a", "Zybook")])
    assert _titles(store) == ["Zybook"]
    assert store.undated_view()["freshness"] == "stale", "no snapshot time, so it cannot be believed"


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


# --- R1: one atomic unit --------------------------------------------------------

def _readonly(store):
    return sqlite3.connect(f"file:{store._test_path}?mode=ro", uri=True)


def _persisted(store):
    """What a separate connection can see: dated titles, receipt time, undated, state."""
    db = _readonly(store)
    try:
        dated = sorted(r[0] for r in db.execute("SELECT title FROM commitments WHERE source='reminders'"))
        import json
        receipt = db.execute("SELECT payload FROM today_source_sync WHERE source='reminders'").fetchone()
        started = json.loads(receipt[0])["snapshot_started_at"] if receipt else None
        undated = sorted(r[0] for r in db.execute("SELECT title FROM assistant_undated_reminders"))
        state = db.execute("SELECT snapshot_started_at,status,total,stored FROM assistant_undated_state").fetchone()
        return dated, started, undated, state
    finally:
        db.close()


def test_r1_a_failure_after_the_dated_rows_rolls_everything_back_and_the_retry_is_valid(store):
    first = _body([_item("u1", "First undated")], ts=10.0, events=[_dated("d1", "First dated")])
    assert _post(first).status_code == 200
    baseline = _persisted(store)
    assert baseline == (["First dated"], 10.0, ["First undated"], (10.0, "complete", 1, 1))

    store._db.execute("CREATE TRIGGER fail_undated BEFORE INSERT ON assistant_undated_reminders "
                      "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    second = _body([_item("u2", "Second undated")], ts=20.0, events=[_dated("d2", "Second dated")])
    failed = _post(second, raise_server_exceptions=False)
    assert failed.status_code == 500
    assert _persisted(store) == baseline, "dated rows, receipt and undated list must roll back together"
    assert [c["title"] for c in store.upcoming(time.time(), days=2)] == ["First dated"]

    store._db.execute("DROP TRIGGER fail_undated")
    retry = _post(second)       # the SAME snapshot time: valid again because no receipt was saved
    assert retry.status_code == 200
    assert _persisted(store) == (["Second dated"], 20.0, ["Second undated"], (20.0, "complete", 1, 1))


def test_r1_a_replay_of_a_committed_snapshot_is_still_refused(store):
    body = _body([_item("u1", "Only")], ts=30.0, events=[_dated("d1", "Dated")])
    assert _post(body).status_code == 200
    again = _body([_item("u2", "Replayed")], ts=30.0, events=[_dated("d2", "Replayed dated")])
    assert _post(again).status_code == 409
    older = _body([_item("u3", "Older")], ts=5.0)
    assert _post(older).status_code == 409
    assert _persisted(store) == (["Dated"], 30.0, ["Only"], (30.0, "complete", 1, 1))


def test_r1_a_failed_dated_write_also_leaves_the_undated_list_untouched(store):
    assert _post(_body([_item("u1", "Kept")], ts=40.0, events=[_dated("d1", "Kept dated")])).status_code == 200
    baseline = _persisted(store)
    store._db.execute("CREATE TRIGGER fail_dated BEFORE INSERT ON commitments "
                      "BEGIN SELECT RAISE(ABORT, 'injected dated failure'); END")
    failed = _post(_body([_item("u9", "New")], ts=50.0, events=[_dated("d9", "New dated")]),
                   raise_server_exceptions=False)
    store._db.execute("DROP TRIGGER fail_dated")
    assert failed.status_code == 500
    assert _persisted(store) == baseline


def test_r1_manual_reminders_survive_a_failed_and_a_retried_sync(store):
    store.add_manual("My own reminder", time.time() + 7200)
    ok = _body([_item("u1", "U")], ts=60.0)
    _post(ok)
    store._db.execute("CREATE TRIGGER fail_undated BEFORE INSERT ON assistant_undated_reminders "
                      "BEGIN SELECT RAISE(ABORT, 'x'); END")
    _post(_body([_item("u2", "V")], ts=70.0), raise_server_exceptions=False)
    store._db.execute("DROP TRIGGER fail_undated")
    _post(_body([_item("u2", "V")], ts=70.0))
    assert "My own reminder" in [c["title"] for c in store.upcoming(time.time(), days=2)]


# --- endpoint behaviour --------------------------------------------------------

def test_authoritative_post_stores_the_list_and_keeps_dated_sync(store):
    r = _post(_body([_item("a", "Work on the Zybook")], events=[_dated("d", "Dated")]))
    assert r.status_code == 200 and r.json()["synced"] == 1
    assert _titles(store) == ["Work on the Zybook"]
    assert [c["title"] for c in store.upcoming(time.time(), days=2)] == ["Dated"]
    assert store.undated_view()["freshness"] == "current"


@pytest.mark.parametrize("variant", ["omitted", "null", "object", "string", "number"])
def test_r4_a_missing_or_malformed_list_leaves_the_stored_one_and_marks_it_stale(store, variant):
    _post(_body([_item("a", "Old title")]))
    later = _body(ts=None)
    if variant != "omitted":
        later["undated"] = {"null": None, "object": {"a": 1}, "string": "text", "number": 7}[variant]
        later["undated_total"] = 1
    assert _post(later).status_code == 200, "the dated part of the read is still accepted"
    assert store.undated_view()["freshness"] == "stale"
    answer = _search()
    assert "Old title" not in answer, "a stale list must not be shown as current"
    assert NOT_INCLUDED in answer


def test_r4_one_invalid_entry_voids_the_whole_payload(store):
    _post(_body([_item("a", "Previous list")]))
    mixed = _body([_item("b", "Looks fine"), {"source_id": "", "title": "no id"}])
    assert _post(mixed).status_code == 200
    assert _titles(store) == ["Previous list"]
    assert store.undated_view()["freshness"] == "stale"
    assert "Looks fine" not in _search() and "Previous list" not in _search()


@pytest.mark.parametrize("total", [None, True, False, "2", -1, 1.5, 0])
def test_r4_a_bad_total_is_not_a_complete_list(store, total):
    _post(_body([_item("a", "Old")]))
    body = _body([_item("x", "New one"), _item("y", "New two")])
    body["undated_total"] = total
    _post(body)
    assert store.undated_view()["freshness"] == "stale"
    assert "New one" not in _search() and NOT_INCLUDED in _search()


def test_r4_an_unavailable_read_leaves_the_list_behind_the_receipt(store):
    _post(_body([_item("a", "Held title")]))
    assert store.undated_view()["freshness"] == "current"
    _post({"source": "reminders", "diagnostics": {**_diag(), "available": False, "reason": "x"}, "events": []})
    assert _titles(store) == ["Held title"]
    assert store.undated_view()["freshness"] == "stale"
    assert "Held title" not in _search()


def test_r4_a_newer_complete_list_repairs_a_stale_one(store):
    _post(_body([_item("a", "Old")]))
    _post(_body())                                  # dated-only read, list now stale
    assert store.undated_view()["freshness"] == "stale"
    _post(_body([_item("b", "Fresh")]))
    assert store.undated_view()["freshness"] == "current"
    answer = _search()
    assert "Fresh" in answer and "Old" not in answer and NOT_INCLUDED not in answer


def test_r4_never_received_reads_as_not_included(store):
    answer = _search("anything")
    assert NOT_INCLUDED in answer and "no active match" in answer
    assert "among reminders with a due date" in answer


def test_r4_a_valid_empty_list_clears_and_reports_plainly(store):
    _post(_body([_item("a", "Soon gone")]))
    _post(_body([]))
    assert store.undated_reminders() == []
    answer = _search("x")
    assert "no active match" in answer and NOT_INCLUDED not in answer
    assert "among reminders with a due date" not in answer, "a complete empty list needs no qualifier"


def test_r4_a_healthy_complete_list_has_no_caveat(store):
    _post(_body([_item("a", "Work on the Zybook"), _item("b", "Get a library card")]))
    answer = _search()
    assert "Apple Reminders with no due date (incomplete):" in answer
    assert "- Work on the Zybook (Reminders)" in answer and "- Get a library card (Reminders)" in answer
    assert "None with a due date." in answer and NOT_INCLUDED not in answer
    assert "showing" not in answer


def test_r4_a_truncated_list_is_reported_as_k_of_n_and_never_as_exhaustive(store):
    items = [_item(f"id{i:03d}", f"Reminder {i:03d}") for i in range(UNDATED_MAX)]
    assert _post(_body(items, total=250)).status_code == 200
    view = store.undated_view()
    assert (view["freshness"], view["status"], view["total"], view["stored"]) == ("current", "truncated", 250, 200)
    listed = _search()
    assert f"showing {UNDATED_MAX} of 250; the rest were not synced" in listed
    miss = _search("zzz-not-there")
    assert "No match among the 200 of 250" in miss and "the other 50 were not checked" in miss
    assert "among reminders with a due date" in miss, "never an unqualified 'no active match'"
    narrow = _search(scope="today")
    assert "250 incomplete Apple reminders with no due date are not in this time window" in narrow
    filtered = _search("Reminder 00", scope="today")
    assert "of the 200 synced" in filtered and "of 250 in all" in filtered


def test_r4_the_backend_caps_an_oversized_list_and_calls_it_truncated(store):
    items = [_item(f"id{i}", f"Reminder {i}") for i in range(300)]
    assert _post(_body(items, total=300)).status_code == 200
    view = store.undated_view()
    assert (view["status"], view["total"], view["stored"]) == ("truncated", 300, UNDATED_MAX)
    assert len(store.undated_reminders()) == UNDATED_MAX


def test_r4_the_state_survives_reopening_the_store(store, tmp_path):
    _post(_body([_item("a", "Persisted")]))
    reopened = AssistantStore(tmp_path / "assistant.db")
    assert reopened.undated_view() == store.undated_view()
    assert reopened.undated_view()["freshness"] == "current"


def test_a_narrow_scope_counts_them_instead_of_hiding_them(store):
    _post(_body([_item("a", "Work on the Zybook"), _item("b", "Get a library card")]))
    answer = _search(scope="today")
    assert "2 incomplete Apple reminders with no due date are not in this time window" in answer
    assert "Work on the Zybook" not in answer


def test_a_query_narrows_the_undated_list(store):
    _post(_body([_item("a", "Work on the Zybook"), _item("b", "Get a library card")]))
    answer = _search("library")
    assert "Get a library card" in answer and "Zybook" not in answer


def test_nothing_anywhere_in_a_current_complete_state_says_no_match(store):
    _post(_body([]))
    assert "no active match" in _search("anything")


def test_undated_are_withheld_when_reminders_cannot_be_verified(store):
    _post(_body([_item("a", "Work on the Zybook")]))
    for readiness in ({"sources": [{"id": "reminders", "state": "unavailable"}]},
                      {"sources": [{"id": "reminders", "state": "ready"}], "reminders_fresh": False},
                      {"sources": [{"id": "reminders", "state": "syncing"}], "reminders_fresh": True}):
        answer = _search(readiness=readiness)
        assert "Work on the Zybook" not in answer
        assert "no due date" not in answer


def test_calendar_posts_cannot_write_the_list(store):
    _post({"source": "calendar", "diagnostics": _diag(), "events": [],
           "undated": [_item("a", "Not a reminder")], "undated_total": 1})
    assert store.undated_reminders() == [] and store.undated_view()["freshness"] == "never"


def test_a_replayed_snapshot_cannot_overwrite_a_newer_list(store):
    fresh = _diag()
    assert _post({"source": "reminders", "diagnostics": fresh, "events": [],
                  "undated": [_item("a", "Newest")], "undated_total": 1}).status_code == 200
    assert _post({"source": "reminders", "diagnostics": dict(fresh), "events": [],
                  "undated": [_item("z", "Replayed")], "undated_total": 1}).status_code == 409
    assert _titles(store) == ["Newest"]


@pytest.mark.parametrize("diagnostics", [
    {"authorized": False, "syncing": False},
    {"authorized": True, "available": False, "reason": "EventKit reported no reminder lists"},
    {"syncing": True},
])
def test_an_unavailable_read_never_touches_the_list(store, diagnostics):
    _post(_body([_item("a", "Keep me")]))
    _post({"source": "reminders", "diagnostics": diagnostics, "events": [],
           "undated": [_item("x", "Must be ignored")], "undated_total": 1})
    assert _titles(store) == ["Keep me"]


# --- review findings: bounds and one coherent read ---------------------------------

@pytest.mark.parametrize("total", [10**30, 2**63, 1_000_001])
def test_an_absurd_total_is_ignored_and_never_crashes_the_sync(store, total):
    _post(_body([_item("a", "Previous")]))
    body = _body([_item("b", "New")], total=total)
    body["events"] = [_dated("d", "Dated survives")]
    response = _post(body, raise_server_exceptions=False)
    assert response.status_code == 200, "an invalid undated payload must not fail the dated sync"
    assert [c["title"] for c in store.upcoming(time.time(), days=2)] == ["Dated survives"]
    assert _titles(store) == ["Previous"] and store.undated_view()["freshness"] == "stale"


def test_field_lengths_are_capped_not_rejected():
    from service.assistant.store import UNDATED_CONTEXT_MAX, UNDATED_TITLE_MAX
    snap = parse_undated_snapshot([{"source_id": "a", "title": "t" * 5000, "context": "c" * 5000}], 1)
    assert len(snap.rows[0][1]) == UNDATED_TITLE_MAX and len(snap.rows[0][2]) == UNDATED_CONTEXT_MAX


def test_the_listing_is_one_coherent_read_of_view_and_rows(store):
    _post(_body([_item("a", "One"), _item("b", "Two")]))
    view, rows = store.undated_listing("")
    assert view["freshness"] == "current" and sorted(r["title"] for r in rows) == ["One", "Two"]
    assert [r["title"] for r in store.undated_listing("tw")[1]] == ["Two"]
    _post(_body())          # a later dated-only read leaves the list behind the receipt
    view, rows = store.undated_listing("")
    assert view["freshness"] == "stale"


def test_long_ids_sharing_a_prefix_merge_instead_of_colliding(store):
    from service.assistant.store import UNDATED_ID_MAX
    prefix = "x" * UNDATED_ID_MAX
    snap = parse_undated_snapshot([{"source_id": prefix + "1", "title": "A"},
                                   {"source_id": prefix + "2", "title": "B"}], 2)
    assert [r[0] for r in snap.rows] == [prefix] and snap.rows[0][1] == "B"
    response = _post(_body([_item(prefix + "1", "A"), _item(prefix + "2", "B")], total=2),
                     raise_server_exceptions=False)
    assert response.status_code == 200 and _titles(store) == ["B"]

