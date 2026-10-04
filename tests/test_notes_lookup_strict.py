"""Named-topic Notes/memory lookup: strict all-term matching and honest freshness.

User report: a freshly created order Note was not retrieved and an unrelated old
record surfaced. Four mechanisms, each pinned here with synthetic data only:

R1  memory/FTS keyword search compiled 'amazon order' to `amazon OR order`, so a
    passage containing only 'order' won. Strict (opt-in) mode needs EVERY
    significant term; non-contiguous terms still match.
R2  an exact/multi-term miss in search_notes fell back to UNRELATED recent notes
    as if relevant. A miss is now a truthful 'no matching note' (the fallback
    survives only for explicit browse requests like 'recent notes').
R3  the Notes cache refreshed once a day and a lookup accepted a ready cached
    snapshot without a fresh read. A lookup can now request a fresh, correlated
    native read and the result says whether the data is current, stale (with age),
    timed out or unavailable. A publish that STARTED before the request is never
    accepted as fresh.
R4  an empty result is a complete answer for that source and says so, so the model
    does not silently fall through to other private tools.

Nothing here touches the real app, Notes, the network, or user data.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-notes-lookup-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ["HOME"] = _scratch.name

from service.assistant import sync_status  # noqa: E402
from service.tools import cache_store, notes_tools  # noqa: E402

FS, RS = notes_tools._FS, notes_tools._RS
NOW = time.time()


def raw(*notes: tuple[float, str, str, str]) -> str:
    return "".join(f"{ts}{FS}{title}{FS}{folder}{FS}{body}{RS}"
                   for ts, title, folder, body in notes)


OLD_ORDER = (NOW - 90 * 86400, "Presentation order", "Work",
             "Speaker order for the offsite: Dana, then Lee, then Priya.")
RECENT_GROCERY = (NOW - 3600, "Groceries", "Home", "milk, eggs, rice")
RECENT_TRIP = (NOW - 7200, "Trip ideas", "Home", "Lisbon in spring")
NEW_AMAZON = (NOW - 60, "Amazon", "Home",
              "Pending delivery. The standing order was placed Friday for the desk lamp.")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(sync_status, "NOTES_REFRESH_TIMEOUT_S", 0.4)
    monkeypatch.setattr(cache_store, "save", lambda name, text: None)
    monkeypatch.setattr(notes_tools, "_notes", "")
    monkeypatch.setattr(notes_tools, "_notes_at", 0.0)
    monkeypatch.setattr(notes_tools, "_available", None)
    monkeypatch.setattr(notes_tools, "_reason", "")
    for name, value in (("_snapshot_at", 0.0), ("_snapshot_started_at", 0.0),
                        ("_received_at", 0.0), ("_receipt_started", 0.0),
                        ("_receipt_available", None),
                        ("_receipt_reason", "")):
        monkeypatch.setattr(notes_tools, name, value, raising=False)


def load(*notes, started=None):
    notes_tools.cache_notes(raw(*notes), snapshot_started_at=started)


def search(**kw) -> str:
    return asyncio.run(notes_tools.search_notes_impl(**kw))


class FakeApp:
    """Plays the Swift app: reacts to a published sync request."""

    def __init__(self, monkeypatch, behaviour):
        from service.assistant.hub import hub
        self.requests: list[float] = []
        self.behaviour = behaviour
        self._sub = hub.subscribe()

        async def publish(event, **kw):
            if event.get("type") == "sync_assistant_sources_now" and "notes" in event["sources"]:
                self.requests.append(time.time())
                await self.behaviour(self)
            return {}

        monkeypatch.setattr(hub, "publish", publish)
        self.hub = hub

    def close(self):
        self.hub.unsubscribe(self._sub)


@pytest.fixture
def app(monkeypatch):
    made: list[FakeApp] = []

    def make(behaviour):
        a = FakeApp(monkeypatch, behaviour)
        made.append(a)
        return a

    yield make
    for a in made:
        a.close()


# --------------------------------------------------------------------- R2 / R1
def test_strict_miss_returns_truthful_no_match_and_no_unrelated_fallback():
    load(OLD_ORDER, RECENT_GROCERY, RECENT_TRIP)
    out = search(query="amazon order", strict=True)
    assert "Groceries" not in out and "Trip ideas" not in out
    assert "Presentation order" not in out, "a note with only 'order' is not an Amazon order"
    assert "no" in out.lower() and "amazon" in out.lower()
    assert "most recent notes" not in out


def test_default_mode_keeps_the_legacy_recent_notes_fallback():
    load(OLD_ORDER, RECENT_GROCERY)
    out = search(query="amazon order")
    assert "no exact match" in out and "Groceries" in out


def test_non_contiguous_terms_still_match():
    load(OLD_ORDER, NEW_AMAZON, RECENT_GROCERY)
    out = search(query="amazon order", strict=True)
    assert "Title: Amazon" in out and "desk lamp" in out
    assert "Presentation order" not in out and "Groceries" not in out


def test_stopwords_are_dropped_from_the_topic():
    load(OLD_ORDER, NEW_AMAZON)
    out = search(query="what's the order for Amazon", strict=True)
    assert "Title: Amazon" in out and "Presentation order" not in out


def test_terms_may_span_title_folder_and_body():
    load((NOW, "Amazon", "Orders", "lamp"), RECENT_GROCERY)
    assert "Title: Amazon" in search(query="amazon orders lamp", strict=True)


def test_strict_does_not_match_a_term_inside_another_word():
    load((NOW, "Border notes", "Home", "crossing the border"), RECENT_GROCERY)
    assert "Border notes" not in search(query="order", strict=True)


def test_vague_browse_request_still_lists_recent_notes():
    load(OLD_ORDER, RECENT_GROCERY, RECENT_TRIP)
    out = search(query="recent notes", strict=True)
    assert "Groceries" in out and "Trip ideas" in out


def test_only_stopword_query_is_a_phrase_lookup_not_a_browse():
    load(RECENT_GROCERY)
    assert "Groceries" not in search(query="the of", strict=True)


# ------------------------------------------------------------------------ R3
def test_fresh_read_finds_a_note_created_after_the_last_snapshot(app):
    load(OLD_ORDER, RECENT_GROCERY, started=NOW - 86400)

    async def behaviour(a):
        notes_tools.cache_notes(raw(OLD_ORDER, RECENT_GROCERY, NEW_AMAZON),
                                snapshot_started_at=time.time())

    fake = app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    assert len(fake.requests) == 1, "a lookup must ask the app for a fresh read"
    assert "Title: Amazon" in out
    assert "may be stale" not in out.lower()


def test_old_in_flight_publish_is_not_accepted_as_fresh(app):
    started_before = NOW - 30

    async def behaviour(a):
        # the read that was already running when the request arrived finishes now
        notes_tools.cache_notes(raw(OLD_ORDER), snapshot_started_at=started_before)

    app(behaviour)
    result = asyncio.run(sync_status.refresh_notes(timeout_seconds=0.4))
    assert result["status"] != "fresh"
    assert result["status"] in {"stale", "timeout"}


def test_unstamped_publish_is_never_proof_of_freshness(app):
    async def behaviour(a):
        notes_tools.cache_notes(raw(OLD_ORDER))

    app(behaviour)
    assert asyncio.run(sync_status.refresh_notes(timeout_seconds=0.3))["status"] != "fresh"


def test_in_flight_publish_then_real_one_is_accepted(app):
    async def behaviour(a):
        notes_tools.cache_notes(raw(OLD_ORDER), snapshot_started_at=NOW - 30)

        async def later():
            await asyncio.sleep(0.15)
            notes_tools.cache_notes(raw(OLD_ORDER, NEW_AMAZON),
                                    snapshot_started_at=time.time())

        a.task = asyncio.get_running_loop().create_task(later())

    app(behaviour)
    result = asyncio.run(sync_status.refresh_notes(timeout_seconds=2.0))
    assert result["status"] == "fresh"


def test_stale_result_reports_age_and_refuses_to_claim_absence(app):
    load(OLD_ORDER, RECENT_GROCERY, started=time.time() - 3 * 3600)
    notes_tools._snapshot_at = time.time() - 3 * 3600

    async def behaviour(a):
        return None  # the app never answers

    app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    low = out.lower()
    assert "3 hours" in low
    assert "can't confirm" in low or "cannot confirm" in low
    assert "groceries" not in low


def test_stale_hit_is_labelled_with_its_age(app):
    load(NEW_AMAZON, started=time.time() - 2 * 3600)
    notes_tools._snapshot_at = time.time() - 2 * 3600

    async def behaviour(a):
        return None

    app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    assert "Title: Amazon" in out and "2 hours" in out


def test_timeout_without_any_snapshot_does_not_say_no_notes(app):
    async def behaviour(a):
        return None

    app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    assert "no note" not in out.lower().replace("no notes found", "")
    assert "could not" in out.lower() or "couldn't" in out.lower()


def test_unavailable_receipt_is_reported_not_treated_as_empty(app):
    load(OLD_ORDER, started=NOW - 86400)

    async def behaviour(a):
        notes_tools.cache_notes("", available=False, reason="Notes access was denied.",
                                snapshot_started_at=time.time())

    app(behaviour)
    result = asyncio.run(sync_status.refresh_notes(timeout_seconds=1.0))
    assert result["status"] == "unavailable"
    out = search(query="amazon order", strict=True, refresh=True)
    assert "Notes access was denied" in out
    assert "no note" not in out.lower()


def test_no_connected_app_is_unavailable_immediately(monkeypatch):
    from service.assistant.hub import hub
    monkeypatch.setattr(hub, "_subs", set())
    t0 = time.monotonic()
    result = asyncio.run(sync_status.refresh_notes(timeout_seconds=5.0))
    assert time.monotonic() - t0 < 1.0
    assert result["status"] == "unavailable"


def test_older_snapshot_arriving_late_does_not_replace_a_newer_one():
    load(NEW_AMAZON, started=NOW)
    notes_tools.cache_notes(raw(OLD_ORDER), snapshot_started_at=NOW - 500)
    assert "Title: Amazon" in search(query="amazon", strict=True)


def test_fresh_miss_is_a_complete_answer_that_forbids_fallthrough(app):
    async def behaviour(a):
        notes_tools.cache_notes(raw(OLD_ORDER, RECENT_GROCERY),
                                snapshot_started_at=time.time())

    app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    assert "just now" in out.lower() or "current" in out.lower()
    assert "other" in out.lower() and "private" in out.lower()


def test_default_search_does_not_request_a_refresh(monkeypatch):
    load(NEW_AMAZON)
    from service.assistant.hub import hub

    async def boom(event, **kw):  # pragma: no cover - must not be hit
        raise AssertionError("refresh requested")

    monkeypatch.setattr(hub, "publish", boom)
    monkeypatch.setattr(notes_tools, "_available", True)
    assert "Title: Amazon" in search(query="amazon")


# ---------------------------------------------------------------- R1: memory
@pytest.fixture
def world(tmp_path, monkeypatch):
    from service.memory.facts import FactStore
    from service.memory.store import SessionStore
    facts = FactStore(tmp_path / "facts.db")
    sessions = SessionStore(tmp_path / "sessions.db")
    import sys
    # `service.memory.store` the module is shadowed by the package attribute `store`.
    monkeypatch.setattr(sys.modules["service.memory.store"], "store", sessions)
    yield facts, sessions
    facts._db.close()
    sessions._db.close()


def _say(sessions, text, sid=None):
    sid = sid or sessions.create_session()
    sessions.add_turn(sid, "user", text)
    return sid


def test_memory_default_retrieval_is_still_or(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    _say(sessions, "Presentation order matters for the offsite.")
    texts = [p["text"] for p in retrieve("amazon order", facts=facts, sessions=sessions)["passages"]]
    assert texts, "legacy OR behaviour must be unchanged unless strict is requested"


def test_memory_strict_requires_every_term(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    _say(sessions, "Presentation order matters for the offsite.")
    _say(sessions, "My Amazon delivery will arrive in the late afternoon, and the order is the desk lamp.")
    result = retrieve("amazon order", facts=facts, sessions=sessions, strict=True)
    texts = [p["text"] for p in result["passages"]]
    assert len(texts) == 1 and "Amazon" in texts[0]


def test_memory_strict_filters_facts_too(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    facts.add("My presentation order is Dana then Lee.", "fact")
    facts.add("My Amazon order is a desk lamp.", "fact")
    got = [f["text"] for f in retrieve("amazon order", facts=facts, sessions=sessions,
                                       strict=True, include_passages=False)["facts"]]
    assert got == ["My Amazon order is a desk lamp."]


def test_memory_strict_drops_stopwords_and_keeps_non_contiguous(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    _say(sessions, "The Amazon thing I need to track is the order for the lamp.")
    got = retrieve("what's the order for Amazon", facts=facts, sessions=sessions, strict=True)
    assert len(got["passages"]) == 1


def test_recall_tool_is_strict_and_states_the_miss_is_complete(world, monkeypatch):
    from service.tools import memory_tools
    facts, sessions = world
    monkeypatch.setattr(memory_tools, "store", facts)
    _say(sessions, "Presentation order matters for the offsite.")
    out = asyncio.run(memory_tools.recall("amazon order"))
    assert "Presentation order" not in out
    assert out.startswith("No matching memory")
    assert "other" in out.lower() and "private" in out.lower()


def test_search_conversations_tool_is_strict(world, monkeypatch):
    from service.tools import memory_tools
    facts, sessions = world
    monkeypatch.setattr(memory_tools, "store", facts)
    _say(sessions, "Presentation order matters for the offsite.")
    out = asyncio.run(memory_tools.search_conversations("amazon order"))
    assert "Presentation order" not in out and out.startswith("No matching")


# ------------------------------------------------------- native reader contract
def test_native_notes_reader_stamps_each_read_and_queues_a_request_behind_one_in_flight():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/Sources/WispApp/NotesReader.swift").read_text()
    # the backend can only trust a post whose read began after its request
    assert '"snapshot_started_at": snapshotStartedAt' in src
    assert "Date().timeIntervalSince1970" in src
    # a request during a running read is queued, not dropped, so a post that
    # started after it exists to be accepted; warm-up/daily reads stay coalesced
    assert "rerunRequested = true" in src and "func syncIfIdle()" in src


# --------------------------------------------------- the HTTP endpoint carries the stamp
def _post_notes(body):
    from fastapi.testclient import TestClient
    import service.main as main
    return TestClient(main.app).post("/assistant/sync/notes", json=body)


def test_endpoint_passes_the_read_start_stamp_to_the_cache():
    response = _post_notes({"raw": raw(OLD_ORDER),
                            "diagnostics": {"available": True, "snapshot_started_at": NOW - 5}})
    assert response.status_code == 200
    assert notes_tools._receipt_started == pytest.approx(NOW - 5)


def test_endpoint_without_a_stamp_is_still_accepted_but_never_fresh(app):
    async def behaviour(a):
        await asyncio.to_thread(_post_notes, {"raw": raw(OLD_ORDER),
                                              "diagnostics": {"available": True}})

    app(behaviour)
    assert asyncio.run(sync_status.refresh_notes(timeout_seconds=0.3))["status"] != "fresh"


def test_freshness_works_end_to_end_through_the_endpoint(app):
    async def behaviour(a):
        await asyncio.to_thread(
            _post_notes, {"raw": raw(OLD_ORDER, NEW_AMAZON),
                          "diagnostics": {"available": True,
                                          "snapshot_started_at": time.time()}})

    app(behaviour)
    assert asyncio.run(sync_status.refresh_notes(timeout_seconds=3.0))["status"] == "fresh"
